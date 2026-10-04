"""Try the final writer on a finished run's report, outside an eval (small cost).

Takes the report (as the draft) and atlas coverage log of one sample of an .eval log,
runs the writer at the given level with a real model against the local corpus, exactly
as `atlas writer pack/check` run in the sandbox, and prints the writer's metadata, the
inputs' size and the final report (or why the draft was kept).

Reader notes come from the sample's `crew_notes` metadata (crew runs only; logs from
before it was recorded have none, so W2 adds nothing for them).

    uv run python tools/writer_check.py logs/atlas-tok200k-v0.3.0/X.eval --sample 0 \\
        --level W3 --data data/verbatim --model openrouter/z-ai/glm-5.3 --provider wafer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from inspect_ai.log import read_eval_log  # noqa: E402
from inspect_ai.model import get_model  # noqa: E402

from messageboard_audit_bench import final_writer as fw  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--sample", type=int, default=0, help="index of the sample in the log")
    ap.add_argument("--level", default="W1", choices=fw.LEVELS)
    ap.add_argument("--data", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", help="pin an OpenRouter provider, no fallbacks")
    ap.add_argument("--max-tokens", type=int, default=20000)
    ap.add_argument("--out", help="write the final report here")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    sample = read_eval_log(args.log).samples[args.sample]
    draft = sample.metadata.get("writer_draft_report") or sample.output.completion
    prompt = sample.input if isinstance(sample.input, str) else sample.input[0].content
    work = Path(tempfile.mkdtemp(prefix="writer-check-"))
    state = work / "coverage.jsonl"
    state.write_text("".join(json.dumps(e) + "\n" for e in sample.metadata.get("atlas_coverage") or []))
    notes = sample.metadata.get("crew_notes") or []
    (work / "notes.jsonl").write_text("".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes))
    local = {p: str(work / Path(p).name) for p in (fw.DRAFT_PATH, fw.CANDIDATE_PATH, fw.INPUTS_PATH)}

    async def write(path, text):
        Path(local[path]).write_text(text)

    async def atlas_json(argv):
        argv = [local.get(a, a) for a in argv]
        out = subprocess.run([str(ROOT / "tools/atlas/bin/atlas"), "--data", args.data, *argv],
                             capture_output=True, text=True,
                             env={**os.environ, "ATLAS_STATE": str(state), "ATLAS_NOTES": str(work / "notes.jsonl")})
        return json.loads(out.stdout)

    model_args = {"provider": {"order": [args.provider], "allow_fallbacks": False}} if args.provider else {}
    model = get_model(args.model, **model_args)
    fw._write = write
    fw._atlas_json = atlas_json
    fw.get_model = lambda role=None, default=None: model
    print(f"[{len(notes)} reader notes from the log]")
    res = asyncio.run(fw.run_writer(
        args.level, prompt, draft,
        min_words=int(sample.metadata.get("report_min_words") or 0),
        max_words=int(sample.metadata.get("report_max_words") or 0),
        tokens_left=lambda: args.max_tokens, deadline=time.monotonic() + 900, install=False))
    print(json.dumps(res.meta, indent=1))
    print(f"\n[inputs and candidate in {work}]")
    if res.report is not None:
        if args.out:
            Path(args.out).write_text(res.report)
            print(f"[final report written to {args.out}]")
        else:
            print("\n===== final report =====\n" + res.report)


if __name__ == "__main__":
    main()
