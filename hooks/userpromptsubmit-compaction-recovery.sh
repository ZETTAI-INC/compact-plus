#!/bin/bash
# UserPromptSubmit hook: detect the marker file left by PostCompact and inject
# compaction recovery guidance through additionalContext (one-shot).
#
# compact-plus-fix v2 (2026-07-23): when the state file is small enough, inject
# its CONTENT directly instead of an instruction to Read it. This removes the
# compliance risk (the post-compact agent ignoring the instruction) and saves a
# Read round-trip. Oversized state files fall back to the path reference.
#
# overhead: one test -f per turn; exit immediately when no marker exists.
# fail-open (always exit 0)

set -uo pipefail

INPUT=$(cat)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
[[ -z "$SESSION_ID" ]] && exit 0

# Do nothing when the marker file is absent.
MARKER_DIR="${TMPDIR:-/tmp}/claude-compacted" # lint:allow-os-tmp
MARKER="$MARKER_DIR/$SESSION_ID"
[[ -f "$MARKER" ]] || exit 0

# Remove the marker so this hook fires only once.
rm -f "$MARKER" 2>/dev/null || true

# Read the active plan path from the session pointer file.
PTR_DIR="${TMPDIR:-/tmp}/claude-active-plan" # lint:allow-os-tmp
PLAN_FILE=""
if [[ -f "$PTR_DIR/$SESSION_ID" ]]; then
  PLAN_FILE=$(cat "$PTR_DIR/$SESSION_ID" 2>/dev/null || true)
  [[ -f "$PLAN_FILE" ]] || PLAN_FILE=""
fi

# Build recovery guidance.
CTX="[COMPACTION RECOVERY] Context compaction occurred. Before resuming work, use the following recovery references."
CTX+=$'\n'

if [[ -n "$PLAN_FILE" ]]; then
  CTX+=$'\n'"- Re-read plan file \`${PLAN_FILE}\` with Read and confirm the current phase and constraints."
  CTX+=$'\n'"- If plan mode is no longer active, note that a plan file exists and ask the user whether to re-enter plan mode."
fi

STATE_DIR="${TMPDIR:-/tmp}/claude-compact-state" # lint:allow-os-tmp
STATE_FILE="$STATE_DIR/$SESSION_ID.md"
INJECT_MAX=$(( ${COMPACT_PLUS_INJECT_MAX_KB:-16} * 1024 ))
if [[ -f "$STATE_FILE" ]]; then
  CTX+=$'\n'"- The state may contain chronological Compact Prep Update notes after the base summary. Read all updates; newer explicit changes and cancellations supersede older statements on the same topic, while unrelated facts remain valid."
  STATE_SIZE=$(wc -c < "$STATE_FILE" 2>/dev/null | tr -d ' ')
  [[ "$STATE_SIZE" =~ ^[0-9]+$ ]] || STATE_SIZE=$((INJECT_MAX + 1))
  if [[ "$STATE_SIZE" -le "$INJECT_MAX" ]]; then
    CTX+=$'\n'"- The pre-compaction state file is injected below (also saved at \`${STATE_FILE}\`). Pay special attention to Session Decisions and Recovery Notes."
    CTX+=$'\n'"----- BEGIN COMPACT-PLUS STATE -----"
    CTX+=$'\n'"$(cat "$STATE_FILE" 2>/dev/null)"
    CTX+=$'\n'"----- END COMPACT-PLUS STATE -----"
  else
    CTX+=$'\n'"- Read state file \`${STATE_FILE}\` with Read and restore the working state."
    CTX+=$'\n'"- Pay special attention to Session Decisions and Recovery Notes."
    if grep -q '^## Skills Invoked' "$STATE_FILE" 2>/dev/null; then
      CTX+=$'\n'"- The state file at \`${STATE_FILE}\` includes a \`## Skills Invoked\` section listing the skills and slash commands invoked earlier in this session."
    fi
  fi
else
  BACKUP_DIR="${HOME}/.claude/backups/transcripts"
  BACKUP_FILE=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "*-${SESSION_ID}.jsonl" -print 2>/dev/null | sort -r | head -n 1 || true)
  if [[ -n "$BACKUP_FILE" && -f "$BACKUP_FILE" ]]; then
    CTX+=$'\n'"- No state file was found. Transcript backup \`${BACKUP_FILE}\` exists; read it if recovery details are needed."
  fi
fi

CTX+=$'\n'"- Check TaskList for the current task list."
CTX+=$'\n'"- Treat next steps from the compaction summary as hypotheses; use the plan and rules as the source of truth."
CTX+=$'\n'"- Treat the compaction summary as a record of prior work, not as instructions for the next action."
CTX+=$'\n'"- Original memory / rule / skill files are the authoritative references; compaction summaries may omit scope qualifiers."
CTX+=$'\n'"- The injected state reflects the moment it was generated; verify file paths and statuses against the current workspace before relying on them."

jq -n --arg ctx "$CTX" '{
  hookSpecificOutput: {
    hookEventName: "UserPromptSubmit",
    additionalContext: $ctx
  }
}'
exit 0
