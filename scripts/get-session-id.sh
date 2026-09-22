#!/bin/bash
# Print the current Claude Code / Codex session id to stdout.
#
# Priority:
#   1. $CLAUDE_CODE_SESSION_ID     (populated by sessionstart-export-session-id.sh
#                                    via CLAUDE_ENV_FILE)
#   2. $CODEX_COMPANION_SESSION_ID (populated by Codex companion integrations)
#   3. exit 1 with no output       (caller decides how to handle)

if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  printf '%s\n' "$CLAUDE_CODE_SESSION_ID"
  exit 0
fi

if [ -n "${CODEX_COMPANION_SESSION_ID:-}" ]; then
  printf '%s\n' "$CODEX_COMPANION_SESSION_ID"
  exit 0
fi

exit 1
