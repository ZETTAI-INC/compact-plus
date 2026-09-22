#!/bin/bash
# PostCompact hook (matcher: ""): record compaction with a marker file.
# PostCompact does not support additionalContext output, so context injection
# is handled by the UserPromptSubmit hook (userpromptsubmit-compaction-recovery.sh).
#
# fail-open (always exit 0)

set -uo pipefail

INPUT=$(cat)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
[[ -z "$SESSION_ID" ]] && exit 0

# Write the marker file. UserPromptSubmit detects it, injects context, then removes it.
MARKER_DIR="${TMPDIR:-/tmp}/claude-compacted" # lint:allow-os-tmp
mkdir -p "$MARKER_DIR" 2>/dev/null || true
printf '%s\n' "$(date +%s)" > "$MARKER_DIR/$SESSION_ID" 2>/dev/null || true

# Reset the compact reminder cooldown after compact runs.
WARN_DIR="${TMPDIR:-/tmp}/claude-compact-warned" # lint:allow-os-tmp
rm -f "$WARN_DIR/$SESSION_ID" 2>/dev/null || true

exit 0
