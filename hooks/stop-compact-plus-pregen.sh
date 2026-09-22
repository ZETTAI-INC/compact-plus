#!/bin/bash
# Stop hook: background pre-generation of the compact-plus state file.
# compact-plus-fix v2 (2026-07-23).
#
# When the raw transcript has grown by more than COMPACT_PLUS_BG_TRIGGER_KB
# since the last state generation, launch precompact-state-summary.sh with
# trigger="background" as a DETACHED process. The synchronous PreCompact run
# then hits its freshness gate and skips the ~90s nested-claude boot.
#
# Runs on Stop (turn end), not UserPromptSubmit, so it never delays prompt
# handling; the wc-based evaluation runs at most once per
# COMPACT_PLUS_BG_COOLDOWN_SEC. Best-effort: a skipped or failed kick just
# means the next qualifying turn end retries, and the synchronous PreCompact
# path remains the fallback.
#
# Windows/msys detach note: a plain `nohup ... &` child inherits the parent's
# Win32 pipe handles regardless of POSIX-level fd redirection, so whatever
# captures this hook's stdout would block until the background generation
# finished (and a timeout tree-kill could reach it). PowerShell Start-Process
# spawns without handle inheritance and outside this process tree; environment
# variables still flow through. Non-Windows environments use nohup.
#
# fail-open (always exit 0), no stdout output.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SUMMARY_SCRIPT="$SCRIPT_DIR/precompact-state-summary.sh"

INPUT=$(cat)
# One jq for both fields — process spawns are the scarce resource on this
# machine, and a failed parse just means "retry at the next turn end".
PARSED=$(printf '%s' "$INPUT" | jq -r '.session_id // "", .transcript_path // ""' 2>/dev/null)
SESSION_ID=${PARSED%%$'\n'*}
TRANSCRIPT_PATH=${PARSED#*$'\n'}
# This machine's jq emits CRLF on multi-line output; a stray \r inside the
# session id silently corrupts every derived path (marker, lock, state file).
SESSION_ID=${SESSION_ID//$'\r'/}
TRANSCRIPT_PATH=${TRANSCRIPT_PATH//$'\r'/}
[[ -n "$SESSION_ID" && "$TRANSCRIPT_PATH" != "$SESSION_ID" ]] || exit 0
[[ -n "$TRANSCRIPT_PATH" && -f "$TRANSCRIPT_PATH" ]] || exit 0
[[ -f "$SUMMARY_SCRIPT" ]] || exit 0

TRIGGER_KB="${COMPACT_PLUS_BG_TRIGGER_KB:-300}"
COOLDOWN="${COMPACT_PLUS_BG_COOLDOWN_SEC:-600}"
[[ "$TRIGGER_KB" =~ ^[0-9]+$ && "$TRIGGER_KB" -gt 0 ]] || exit 0

STATE_DIR="${TMPDIR:-/tmp}/claude-compact-state" # lint:allow-os-tmp
OFFSET_FILE="${TMPDIR:-/tmp}/claude-compact-state-offset/$SESSION_ID" # lint:allow-os-tmp
KICK_DIR="${TMPDIR:-/tmp}/claude-compact-state-kick" # lint:allow-os-tmp
KICK_FILE="$KICK_DIR/$SESSION_ID"
LOCK_DIR="${TMPDIR:-/tmp}/claude-compact-state-lock/$SESSION_ID.lock" # lint:allow-os-tmp

# A generation is already in flight.
[[ -d "$LOCK_DIR" ]] && exit 0

printf -v NOW '%(%s)T' -1
if [[ -f "$KICK_FILE" ]]; then
  LAST=$(cat "$KICK_FILE" 2>/dev/null || printf '0')
  [[ "$LAST" =~ ^[0-9]+$ ]] || LAST=0
  [[ $((NOW - LAST)) -lt "$COOLDOWN" ]] && exit 0
fi

# Record the evaluation time first so the wc below runs at most once per
# cooldown window even when no kick happens.
mkdir -p "$KICK_DIR" 2>/dev/null || exit 0
printf '%s\n' "$NOW" > "$KICK_FILE" 2>/dev/null || true

SIZE=$(wc -c < "$TRANSCRIPT_PATH" 2>/dev/null | tr -d ' ')
[[ "$SIZE" =~ ^[0-9]+$ ]] || exit 0
OFFSET=0
if [[ -f "$OFFSET_FILE" ]]; then
  OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null || printf '0')
  [[ "$OFFSET" =~ ^[0-9]+$ ]] || OFFSET=0
fi
if [[ "$SIZE" -ge "$OFFSET" ]]; then
  # Below the growth threshold: state is still fresh enough, nothing to do.
  [[ $((SIZE - OFFSET)) -lt $((TRIGGER_KB * 1024)) ]] && exit 0
fi
# SIZE < OFFSET means the transcript was replaced -> state is stale: kick.

mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
KICK_JSON="$STATE_DIR/kick-$SESSION_ID.json"
BG_LOG="$STATE_DIR/bg-$SESSION_ID.log"
# Synthesize the JSON with builtins only (no jq spawn to flake under load).
# Escape backslashes then quotes; hook-provided paths contain no newlines.
ESC_SID=${SESSION_ID//\\/\\\\}; ESC_SID=${ESC_SID//\"/\\\"}
ESC_TP=${TRANSCRIPT_PATH//\\/\\\\}; ESC_TP=${ESC_TP//\"/\\\"}
printf '{"session_id":"%s","transcript_path":"%s","trigger":"background","custom_instructions":""}\n' \
  "$ESC_SID" "$ESC_TP" > "$KICK_JSON" 2>/dev/null || exit 0

if command -v powershell.exe >/dev/null 2>&1 && command -v cygpath >/dev/null 2>&1; then
  WRAP="$STATE_DIR/kickwrap-$SESSION_ID.sh"
  {
    printf 'export COMPACT_PLUS_EFFORT=%q\n' "${COMPACT_PLUS_BG_EFFORT:-high}"
    printf 'exec bash %q < %q > %q 2>&1\n' "$SUMMARY_SCRIPT" "$KICK_JSON" "$BG_LOG"
  } > "$WRAP" 2>/dev/null || exit 0
  BASH_W=$(cygpath -w "$(command -v bash)" 2>/dev/null) || exit 0
  WRAP_W=$(cygpath -w "$WRAP" 2>/dev/null) || exit 0
  powershell.exe -NoProfile -NonInteractive -Command \
    "Start-Process -WindowStyle Hidden -FilePath '$BASH_W' -ArgumentList '$WRAP_W'" \
    >/dev/null 2>&1 || true
else
  COMPACT_PLUS_EFFORT="${COMPACT_PLUS_BG_EFFORT:-high}" \
    nohup bash "$SUMMARY_SCRIPT" < "$KICK_JSON" > "$BG_LOG" 2>&1 &
  disown 2>/dev/null || true
fi

exit 0
