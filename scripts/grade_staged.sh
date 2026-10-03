#!/usr/bin/env bash
# Grade a folder of staged reports (benchmark/graded_inputs/<dir> or an absolute path)
# against the findings (v2) and TL;DR (tldrh) sheets, both at once.
#
#   scripts/grade_staged.sh DIR [JUDGE] [EFFORT] [LOG_DIR]
#   scripts/grade_staged.sh /path/to/staged openai/gpt-6.1-sol medium logs/grading-x
#
# Sheets within a report are graded concurrently by the scorer; MAX_CONNECTIONS (default
# 32) bounds the judge calls in flight. EFFORT defaults to xhigh, the published setting;
# lower efforts are faster and cheaper but score differently, so compare only grades
# made with the same judge and effort.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="${1:?staged report folder}"; JUDGE="${2:-anthropic/claude-opus-5-5}"
EFFORT="${3:-xhigh}"; LOGS="${4:-logs/grading}"; CONN="${MAX_CONNECTIONS:-32}"
mkdir -p "$LOGS"
env_args=(); [[ ! -f .env ]] || env_args=(--env-file .env)
pids=()
for rubric in v2 tldrh; do
  uv run "${env_args[@]}" inspect eval messageboard_audit_bench/german_wiki_report_grade \
    -T "dir=$DIR" -T "rubric=$rubric" -T "judge=$JUDGE" -T "judge_effort=$EFFORT" \
    --log-dir "$LOGS" --log-model-api --max-connections "$CONN" --display plain \
    > "$LOGS/$rubric.out" 2>&1 &
  pids+=($!)
done
status=0
for pid in "${pids[@]}"; do wait "$pid" || status=1; done
echo "grading logs: $LOGS (exit $status)"
exit $status
