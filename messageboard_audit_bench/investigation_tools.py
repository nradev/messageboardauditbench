"""Optional investigation tools for the native ReAct agent (``-T tools=atlas``).

``atlas`` (``tools/atlas``) is copied into the sandbox at sample start, so the image is
unchanged and baseline runs are byte-identical. The agent gets it two ways: an ``atlas``
tool whose description carries the command list, and an ``atlas`` executable on PATH for
use from bash. Its coverage log is read back into sample metadata as a process metric.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from inspect_ai.tool import Tool, tool
from inspect_ai.util import sandbox

from messageboard_audit_bench.runtime import repo_root

SUPPORTED_TOOLS = ("atlas", "crew")
# Everything runs as the sandbox's only permitted user (uid 1000, HOME=/home/agent).
ATLAS_HOME = "/home/agent/.local/share/atlas"
ATLAS_BIN = "/home/agent/.local/bin/atlas"
ATLAS_STATE = "/tmp/atlas-coverage.jsonl"
ATLAS_DATA = "/work/data"
_WRAPPER = f"""#!/bin/sh
ATLAS_DATA="${{ATLAS_DATA:-{ATLAS_DATA}}}" ATLAS_STATE="${{ATLAS_STATE:-{ATLAS_STATE}}}" \\
ATLAS_CACHE="${{ATLAS_CACHE:-/tmp/atlas-cache}}" PYTHONPATH="{ATLAS_HOME}" exec python3 -m atlas "$@"
"""


# Appended to the task prompt when the tool is enabled. It says how to use the tool and
# nothing about what to look for, so it is the same for every incident.
TOOL_PROMPTS = {
    "atlas": (
        "\n\nYou also have `atlas`, a map of the log corpus (an `atlas` tool, also on PATH "
        "in bash). Use it: start with `atlas overview` to see the corpus's structure. Its "
        "themes show what is typical (topics shared by many records and actors); its rare "
        "records, `atlas unseen`, `atlas entities` and `atlas anomalies` (look-alike names, "
        "mixed-script words, bursts) show what is unusual, and each rare "
        "record says whether it belongs to a theme or is isolated. Drill in with "
        "`atlas expand`, `atlas grep` and `atlas show`, and run `atlas unseen` "
        "periodically and before you finalise your report, to find salient records you have "
        "not looked at yet. For questions about one value or one field (who, when, how "
        "often), use `atlas entities`, `atlas pivot`, `atlas count`, `atlas rows` and "
        "`atlas join` before writing a script. Before you finalise, run `atlas gapcheck` on "
        "your draft: correct every Fix item (a citation or quote the data does not support "
        "as written); Consider items are optional, so include one only if it is material to "
        "your account. Plain shell tools remain available for anything atlas does not cover.\n"
    ),
    "crew": (
        "\n\nYou also have a reading crew (`crew_sweep`, `crew_brief`, `crew_ask`, `crew_notes`). Within your budget you can read only a small "
        "part of the corpus yourself; the crew's readers read 60 to 150 records in parallel "
        "outside your turns — not instantly, but far faster than the dozens of turns reading "
        "them yourself would take. They return short notes, each with a record ref and an exact quote checked against the "
        "record. Use it whenever reading more would change your account: `crew_sweep` early "
        "on, for a cross-section of the corpus beyond what atlas lists (each call moves on to "
        "records not read yet); `crew_brief` on a set to understand a theme, cluster or event in "
        "depth instead of sampling a few records; `crew_ask` with a set and a question to check every "
        "record in a set (grep hits, filtered rows, a theme) for the answer, including whether "
        "something never happens. Sets are atlas ids (tNN, cNN, wNN), grep:REGEX, pivot:VALUE, "
        "rows:TABLE with filters, around:REF or refs:A,B. Readers see only the records given to "
        "them and can miss things, so confirm what you rely on with `atlas show`.\n"
    ),
}

# Added to the continue messages sent when the agent tries to finish before the minimum
# investigation time, naming only the tools of this arm. Baseline runs (no tools) keep the
# published wording.
CONTINUE_HINTS = {
    "atlas": "`atlas unseen` lists salient records you have not looked at",
    "crew": "`crew_sweep` has readers go through records you have not read yet, "
            "`crew_ask` checks a whole set against a question, and `crew_notes` lists what "
            "readers noted that you have not seen yet",
}


def continue_hint(tools: tuple[str, ...]) -> str:
    hints = [CONTINUE_HINTS[t] for t in tools if t in CONTINUE_HINTS]
    if not hints:
        return ""
    return (
        " Use the remaining time to widen the investigation rather than polish wording: look "
        "for activity, actors, periods or explanations your report does not cover yet. "
        + "For example: " + "; ".join(hints) + "."
    )


def prompt_addendum(tools: tuple[str, ...]) -> str:
    return "".join(TOOL_PROMPTS[t] for t in tools)


def parse_tools(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    # Inspect's CLI turns `-T tools=atlas,crew` into a list; Python callers pass a string.
    parts = value if isinstance(value, (list, tuple)) else (value or "").split(",")
    names = tuple(sorted({str(n).strip() for n in parts if str(n).strip()}))
    unknown = [n for n in names if n not in SUPPORTED_TOOLS]
    if unknown:
        raise ValueError(f"unknown investigation tools {unknown}; choose from {', '.join(SUPPORTED_TOOLS)}")
    if "crew" in names and "atlas" not in names:
        raise ValueError("tools=crew needs atlas too (tools=atlas,crew): readers get their records from atlas")
    return names


def _atlas_sources() -> dict[str, str]:
    pkg = repo_root() / "tools" / "atlas" / "atlas"
    return {p.name: p.read_text() for p in sorted(Path(pkg).glob("*.py"))}


async def install_atlas() -> dict:
    """Copy atlas into the sandbox, put it on PATH, and build its index once."""
    for name, text in _atlas_sources().items():
        await sandbox().write_file(f"{ATLAS_HOME}/atlas/{name}", text)
    # `bash --login` (Inspect's bash tool) reads ~/.profile; put ~/.local/bin on PATH there.
    installed = await sandbox().exec(
        ["sh", "-c", f"mkdir -p $(dirname {ATLAS_BIN}) && cat > {ATLAS_BIN} && chmod 755 {ATLAS_BIN} "
                     "&& echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> $HOME/.profile"],
        input=_WRAPPER,
    )
    if not installed.success:
        raise RuntimeError(f"could not install atlas: {installed.stderr[:500]}")
    # Warm the index as the agent's user, then clear the coverage log so it only
    # records the agent's own calls.
    warm = await sandbox().exec(["sh", "-c", f"{ATLAS_BIN} overview >/dev/null && rm -f {ATLAS_STATE}"], timeout=300)
    if not warm.success:
        raise RuntimeError(f"atlas index build failed: {warm.stderr[:500]}")
    return {"atlas_sources": sorted(_atlas_sources())}


GAPCHECK_FRAMING = (
    "Automatic check of your draft report.md against the data (atlas gapcheck). Corrections "
    "first: fix every Fix item below; each is a citation or quote the data does not support "
    "as written. Consider items are optional: include one only if it is material to your "
    "account; leaving out immaterial items is correct, and `atlas gapcheck --dismiss gID` sets "
    "an item aside. Then continue your work.\n\n"
)


async def auto_gapcheck() -> tuple[str | None, dict]:
    """Run `atlas gapcheck` on the draft report. Returns (message for the agent, metadata),
    or (None, {...}) when there is no report yet, so the caller can try again later."""
    result = await sandbox().exec([ATLAS_BIN, "gapcheck", "/work/report.md"], timeout=120)
    out = (result.stdout or "").strip()
    m = re.search(r": (\d+) to fix, (\d+) to consider", out.splitlines()[0] if out else "")
    if not result.success or not m:
        return None, {"error": (out or result.stderr or "")[:300]}
    return GAPCHECK_FRAMING + out, {"fix": int(m.group(1)), "consider": int(m.group(2))}


async def read_atlas_coverage() -> list[dict]:
    try:
        raw = await sandbox().read_file(ATLAS_STATE)
    except Exception:
        return []
    out = []
    for line in raw.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@tool
def atlas() -> Tool:
    async def execute(command: str) -> str:
        """Map of the log corpus in /work/data: clusters of near-duplicate records, the rare
        and unusual records ranked first, and a record of what you have already looked at.
        Use it to orient yourself quickly and to find the rare records that plain grep buries
        under repetition. Start with `overview`. It is also on PATH in bash as `atlas`.

        Commands:
          overview                       start here: files, fields, themes (what is typical), rare records
          themes [--field T.F]           topics shared by many records and actors (tNN)
          profile [TABLE]                fields: roles, counts, top/rare values, time range and precision
          clusters [--field T.F] [--sort salience|size|time] [--page N]
          expand ID                      open a theme (tNN), cluster (cNN) or window (wNN): span, actors, examples
          show REF [--offset N]          one row in full; REF = file:line, the record's own id, or a
                                         cluster/theme id (its first record)
          grep PATTERN [-i] [--field T.F] [--page N]   regex search, hits grouped by cluster, rare hits first
          unseen                         rare records not opened yet, new ones first (each call moves
                                         on), plus unopened themes and coverage so far
          entities [--kind K] [--sort rare|count|first]   values to pivot on (field values, hosts, IPs,
                                         paths...), rarest first, with first/last seen and actors
          pivot VALUE [--exact]          every row in any file containing VALUE, as one timeline
          count TABLE[.FIELD] [--where F=V|F!=V|F~RE|F>V|F<=V ...] [--by day|hour|FIELD]   filtered counts and
                                         group-bys, instead of writing a script
          rows TABLE [--where ...] [--fields a,b] [--sort time|FIELD] [--desc]   matching rows, one
                                         line each with their ids, instead of writing a script
          join A.FIELD B.FIELD [-i]      which values of one field appear in another (overlap, examples)
          timeline                       when activity starts, ends, peaks, changes level, goes quiet
          anomalies                      look-alike identifiers (confusable characters, e.g. a Cyrillic
                                         letter in a name), mixed-script words, actor and record bursts
          gapcheck [REPORT]              check your report against the data: Fix (citations or quotes
                                         the data does not support) and Consider (optional coverage
                                         questions; leave out what is immaterial); --dismiss gID

        Refs like logs:120 are 1-based line numbers in the source file, so they stay valid in
        shell and python too; rows also show their own id field, which is best for citing.

        Args:
            command: An atlas command line without the leading "atlas", e.g. "overview",
                "expand c12", "grep -i 'timeout|retry'".
        """
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"could not parse command: {e}"
        if argv and argv[0] == "atlas":
            argv = argv[1:]
        result = await sandbox().exec([ATLAS_BIN, *argv], timeout=300)
        out = result.stdout if result.success else f"{result.stdout}\n{result.stderr}"
        return out.strip() or "(no output)"

    return execute
