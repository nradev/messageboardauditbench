"""Check the reading crew's reader output on a local corpus, outside an eval (small cost).

Runs `crew_brief` (or `crew_ask` with --ask "question") on each given set with a real model and prints the output and the reader
stats: how many notes came back, how many quotes verified, latency, tokens. Records come
from the local atlas, exactly as `atlas records` gives them in the sandbox.

    uv run python tools/crew_check.py --data data/verbatim t1 t3 grep:proxy \\
        --model openrouter/z-ai/glm-5.3 --provider wafer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from inspect_ai.model import get_model  # noqa: E402

from messageboard_audit_bench import reading_crew as rc  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sets", nargs="+", help="atlas sets: tNN, cNN, wNN, grep:RE, pivot:V, rows:T, around:REF, or sweep")
    ap.add_argument("--data", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", help="pin an OpenRouter provider, no fallbacks")
    ap.add_argument("--where", action="append", default=[])
    ap.add_argument("--ask", help="run `crew_ask` with this question instead of `crew_brief`")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    model_args = {"provider": {"order": [args.provider], "allow_fallbacks": False}} if args.provider else {}
    model = get_model(args.model, **model_args)
    state = Path(tempfile.gettempdir()) / "crew-check-coverage.jsonl"  # atlas logs here; not used

    async def fetch(spec, where, limits):
        argv = [str(ROOT / "tools/atlas/bin/atlas"), "--data", args.data, "records", spec,
                "--limit", str(limits.set_records), "--chars", str(limits.record_chars)]
        for w in where:
            argv += ["--where", w]
        out = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, "ATLAS_STATE": str(state)})
        return json.loads(out.stdout)

    async def mark(refs, label):
        return None

    rc.fetch_records = fetch
    rc.mark_read = mark
    crew = rc.Crew(model=model)
    crew.stats.reader_model = args.model

    async def run_all():
        for spec in args.sets:
            before = (crew.stats.notes_returned, crew.stats.notes_verified)
            print(f"\n===== {spec} =====")
            if spec == "sweep":
                print(await rc.sweep(crew))
            else:
                print(await (rc.ask(crew, spec, args.ask, args.where) if args.ask else rc.brief(crew, spec, args.where)))
            got = crew.stats.notes_returned - before[0]
            ok = crew.stats.notes_verified - before[1]
            print(f"[notes returned {got}, verified {ok} ({ok / got:.0%})]" if got else "[no notes returned]")

    asyncio.run(run_all())
    print("\n===== reader stats =====")
    print(json.dumps(crew.stats.metadata(), indent=1))


if __name__ == "__main__":
    main()
