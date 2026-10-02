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
        under repetition. It is also on PATH in bash as `atlas`.

        Commands:
          overview                       start here: files, guessed fields, biggest and most salient clusters
          profile [TABLE]                fields: roles, counts, top/rare values, time range and precision
          clusters [--field T.F] [--sort salience|size|time] [--page N]
          expand ID                      open a cluster (cNN) or window (wNN): span, actors, varied examples
          show REF [--offset N]          one row in full (REF = table:line, e.g. revisions:120)
          grep PATTERN [-i] [--field T.F] [--page N]   regex search, hits grouped by cluster, rare hits first
          unseen [--page N]              salient clusters you have not opened yet, plus coverage so far

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
