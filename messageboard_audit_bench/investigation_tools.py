"""Optional investigation tools for the native ReAct agent (``-T tools=atlas``).

``atlas`` (``tools/atlas``) is copied into the sandbox at sample start, so the image is
unchanged and baseline runs are byte-identical. The agent gets it two ways: an ``atlas``
tool whose description carries the command list, and an ``atlas`` executable on PATH for
use from bash. Its coverage log is read back into sample metadata as a process metric.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from inspect_ai.tool import Tool, tool
from inspect_ai.util import sandbox

from messageboard_audit_bench.runtime import repo_root

SUPPORTED_TOOLS = ("atlas",)
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
        "in bash). Use it: start with `atlas overview` to see the corpus's structure, its "
        "largest clusters of repeated records and its rarest, most unusual ones. Drill in "
        "with `atlas expand`, `atlas grep` and `atlas show`, and run `atlas unseen` "
        "periodically and before you finalise your report, to find salient records you have "
        "not looked at yet. For questions about one value or one field (who, when, how "
        "often), use `atlas entities`, `atlas pivot` and `atlas count` before writing a "
        "script. Plain shell tools remain available for anything atlas does not cover.\n"
    ),
}


def prompt_addendum(tools: tuple[str, ...]) -> str:
    return "".join(TOOL_PROMPTS[t] for t in tools)


def parse_tools(value: str | None) -> tuple[str, ...]:
    names = tuple(sorted({n.strip() for n in (value or "").split(",") if n.strip()}))
    unknown = [n for n in names if n not in SUPPORTED_TOOLS]
    if unknown:
        raise ValueError(f"unknown investigation tools {unknown}; choose from {', '.join(SUPPORTED_TOOLS)}")
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
          overview                       start here: files, guessed fields, biggest and most salient clusters
          profile [TABLE]                fields: roles, counts, top/rare values, time range and precision
          clusters [--field T.F] [--sort salience|size|time] [--page N]
          expand ID                      open a cluster (cNN) or window (wNN): span, actors, varied examples
          show REF [--offset N]          one row in full; REF = file:line or the record's own id
          grep PATTERN [-i] [--field T.F] [--page N]   regex search, hits grouped by cluster, rare hits first
          unseen [--page N]              salient clusters you have not opened yet, plus coverage so far
          entities [--kind K] [--sort rare|count|first]   values to pivot on (field values, hosts, IPs,
                                         paths...), rarest first, with first/last seen and actors
          pivot VALUE [--exact]          every row in any file containing VALUE, as one timeline
          count TABLE[.FIELD] [--where F=V|F!=V|F~RE ...] [--by day|hour|FIELD]   filtered counts and
                                         group-bys, instead of writing a script

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
