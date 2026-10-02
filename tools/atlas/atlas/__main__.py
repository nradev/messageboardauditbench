import sys

from .cli import main

try:
    code = main()
    sys.stdout.flush()
except BrokenPipeError:  # output piped into head/less that closed early
    sys.stderr.close()
    code = 0
raise SystemExit(code)
