#!/usr/bin/env bash
# PreToolUse hook: hard-block any Write/Edit/Bash targeting frozen strategy files.
# Reads the hook JSON payload from stdin; exit code 2 blocks the tool call
# and feeds stderr back to Claude as the reason. No jq dependency — pure python3.

HOOK_PAYLOAD="$(cat)" python3 - <<'PY'
import json, os, re, sys

try:
    payload = json.loads(os.environ.get("HOOK_PAYLOAD", "") or "{}")
except Exception:
    sys.exit(0)  # unparseable payload -> don't block

tool_input = payload.get("tool_input", {}) or {}
file_path = tool_input.get("file_path", "") or ""
command = tool_input.get("command", "") or ""

FROZEN_FILE = re.compile(r"(^|/)strategy\.py$")
# bash writes: redirection, in-place sed, tee, mv/cp targeting strategy.py
FROZEN_BASH = re.compile(r"(>|>>|sed\s+-i|tee\s|mv\s|cp\s)[^\n]*strategy\.py")

if file_path and FROZEN_FILE.search(file_path):
    print("BLOCKED: strategy.py is frozen. Changes require head-to-head backtest "
          "validation (research harness) and manual human edit. See CLAUDE.md.",
          file=sys.stderr)
    sys.exit(2)

if command and FROZEN_BASH.search(command):
    print("BLOCKED: bash command appears to modify strategy.py, which is frozen. "
          "Read access is fine; writes are not.", file=sys.stderr)
    sys.exit(2)

sys.exit(0)
PY
exit $?
