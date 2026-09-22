"""Integration checks using /bin/bash (Bash 3.2 on stock macOS).

Run: python3 -m unittest discover -s tests -v
No live LLM calls or changes to the user's Claude settings are made.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="compact-plus test ")
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.transcript = self.tmp / "transcript 日本語.jsonl"
        self.transcript.write_text(json.dumps({
            "type": "user", "message": {"content": "test " * 400}
        }) + "\n")
        self.payload = json.dumps({
            "session_id": "macos-test", "transcript_path": str(self.transcript)
        })
        self.env = dict(os.environ, TMPDIR=str(self.tmp),
                        COMPACT_PLUS_PRIMARY_BACKEND="printf '# Compact Prep State\\n## Active Plan\\nTest plan\\n'",
                        COMPACT_PLUS_FALLBACK_BACKEND="",
                        COMPACT_PLUS_BG_TRIGGER_KB="1",
                        COMPACT_PLUS_BG_COOLDOWN_SEC="600")

    def run_hook(self, name):
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "hooks" / name)],
            input=self.payload, text=True, capture_output=True,
            env=self.env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_stop_cooldown_uses_valid_epoch(self):
        # Below the threshold: evaluate and record the time without spawning.
        self.env["COMPACT_PLUS_BG_TRIGGER_KB"] = "100"
        before = int(time.time())
        self.run_hook("stop-compact-plus-pregen.sh")
        kick = self.tmp / "claude-compact-state-kick" / "macos-test"
        stamp = int(kick.read_text())
        self.assertGreaterEqual(stamp, before)
        self.assertLessEqual(stamp, int(time.time()))
        # A recent evaluation must prevent launching even above the threshold.
        self.env["COMPACT_PLUS_BG_TRIGGER_KB"] = "1"
        self.run_hook("stop-compact-plus-pregen.sh")
        self.assertEqual(int(kick.read_text()), stamp)
        self.assertFalse((self.tmp / "claude-compact-state" / "kick-macos-test.json").exists())

    def append_event(self, text):
        with self.transcript.open("a") as stream:
            stream.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")

    def capture_backend_prompt(self):
        self.env["COMPACT_PLUS_PRIMARY_BACKEND"] = (
            'cat > "$TMPDIR/backend-prompt"; '
            'printf "%s" "$SYSTEM_PROMPT" > "$TMPDIR/backend-system"; '
            'printf "%s" "$COMPACT_PLUS_EFFORT" > "$TMPDIR/backend-effort"; '
            'case "$SYSTEM_PROMPT" in *"Compact Plus Delta Writer"*) '
            "printf '## Compact Prep Update\\n- Deployment cancelled.\\n';; "
            "*) printf '# Compact Prep State\\n## Active Plan\\nUpdated plan\\n';; esac")

    def test_small_last_minute_change_is_saved(self):
        self.run_hook("precompact-state-summary.sh")
        self.append_event("Cancel the deployment; keep the changes local.")
        self.capture_backend_prompt()
        # Even a configured large threshold must not hide the final decision.
        self.env["COMPACT_PLUS_FRESH_DELTA_KB"] = "999999"
        self.run_hook("precompact-state-summary.sh")
        prompt = (self.tmp / "backend-prompt").read_text()
        self.assertIn("mode: delta", prompt)
        self.assertIn("Cancel the deployment", prompt)
        self.assertNotIn("Test plan", prompt)
        self.assertNotIn("test test", prompt)
        state = (self.tmp / "claude-compact-state/macos-test.md").read_text()
        self.assertTrue(state.startswith("# Compact Prep State\n## Active Plan\nTest plan\n"))
        self.assertIn("## Compact Prep Update\n- Deployment cancelled.", state)
        self.assertEqual((self.tmp / "backend-effort").read_text(), "low")
        self.assertEqual((self.tmp / "claude-compact-state-counter/macos-test").read_text().strip(), "2")
        self.assertEqual(int((self.tmp / "claude-compact-state-offset/macos-test").read_text()),
                         self.transcript.stat().st_size)

    def test_changes_while_waiting_for_background_are_saved(self):
        self.run_hook("precompact-state-summary.sh")
        self.append_event("First change")
        lock = self.tmp / "claude-compact-state-lock/macos-test.lock"
        lock.mkdir()
        self.capture_backend_prompt()

        def finish_background():
            self.append_event("Final change during background generation")
            lock.rmdir()

        timer = threading.Timer(0.2, finish_background)
        timer.start()
        try:
            self.run_hook("precompact-state-summary.sh")
        finally:
            timer.join()
        self.assertIn("Final change during background generation",
                      (self.tmp / "backend-prompt").read_text())
        self.assertEqual(int((self.tmp / "claude-compact-state-offset/macos-test").read_text()),
                         self.transcript.stat().st_size)

    def test_custom_instructions_force_update_without_new_events(self):
        self.run_hook("precompact-state-summary.sh")
        payload = json.loads(self.payload)
        payload["custom_instructions"] = "Preserve the cancellation decision"
        self.payload = json.dumps(payload)
        self.capture_backend_prompt()
        self.run_hook("precompact-state-summary.sh")
        self.assertIn(payload["custom_instructions"], (self.tmp / "backend-prompt").read_text())

    def test_backend_failure_does_not_mark_new_events_saved(self):
        self.run_hook("precompact-state-summary.sh")
        offset = self.tmp / "claude-compact-state-offset/macos-test"
        previous = offset.read_text()
        self.append_event("Important unsaved change")
        self.env["COMPACT_PLUS_PRIMARY_BACKEND"] = "exit 1"
        self.run_hook("precompact-state-summary.sh")
        self.assertEqual(offset.read_text(), previous)
        self.assertFalse((self.tmp / "claude-compact-state-lock/macos-test.lock").exists())

    def test_repeated_updates_recover_and_background_consolidates(self):
        self.run_hook("precompact-state-summary.sh")
        self.capture_backend_prompt()
        # A refresh cycle must not force a full rewrite during compaction.
        self.env["COMPACT_PLUS_INCREMENTAL_REFRESH"] = "1"
        for event in ("Cancel deployment", "Keep all changes local"):
            self.append_event(event)
            self.run_hook("precompact-state-summary.sh")
        state = self.tmp / "claude-compact-state/macos-test.md"
        saved = state.read_text()
        self.assertEqual(saved.count("## Compact Prep Update"), 2)
        prompt = (self.tmp / "backend-prompt").read_text()
        self.assertIn("Keep all changes local", prompt)
        self.assertNotIn("Cancel deployment", prompt)
        # No changes means no backend call and no duplicate update.
        (self.tmp / "backend-prompt").unlink()
        self.run_hook("precompact-state-summary.sh")
        self.assertFalse((self.tmp / "backend-prompt").exists())
        self.assertEqual(state.read_text(), saved)
        self.run_hook("compaction-recovery.sh")
        ctx = json.loads(self.run_hook("userpromptsubmit-compaction-recovery.sh"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(saved.strip(), ctx)
        self.assertIn("newer explicit changes and cancellations supersede", ctx)
        # Oversized state uses a file reference with the same precedence rule.
        self.env["COMPACT_PLUS_INJECT_MAX_KB"] = "0"
        self.run_hook("compaction-recovery.sh")
        ctx = json.loads(self.run_hook("userpromptsubmit-compaction-recovery.sh"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Read state file", ctx)
        self.assertIn("Read all updates", ctx)
        payload = json.loads(self.payload)
        payload["trigger"] = "background"
        self.payload = json.dumps(payload)
        self.run_hook("precompact-state-summary.sh")
        self.assertIn(saved.strip(), (self.tmp / "backend-prompt").read_text())
        self.assertNotIn("## Compact Prep Update", state.read_text())
        self.assertIn("Updated plan", state.read_text())

    def test_large_delta_keeps_early_decisions(self):
        self.run_hook("precompact-state-summary.sh")
        self.append_event("Critical cancellation at start of delta")
        self.append_event("later context " * 4000)
        self.capture_backend_prompt()
        self.env["COMPACT_PLUS_TRANSCRIPT_TAIL_KB"] = "1"
        self.run_hook("precompact-state-summary.sh")
        prompt = (self.tmp / "backend-prompt").read_text()
        self.assertIn("Critical cancellation at start of delta", prompt)
        self.assertIn("later context", prompt)

    def test_invalid_delta_output_keeps_state_and_offset(self):
        self.run_hook("precompact-state-summary.sh")
        state = self.tmp / "claude-compact-state/macos-test.md"
        offset = self.tmp / "claude-compact-state-offset/macos-test"
        previous = (state.read_text(), offset.read_text())
        self.append_event("Important new decision")
        # A full-state answer is not a valid delta and must not overwrite state.
        self.run_hook("precompact-state-summary.sh")
        self.assertEqual((state.read_text(), offset.read_text()), previous)

    def test_events_arriving_during_generation_remain_unsaved(self):
        self.run_hook("precompact-state-summary.sh")
        self.append_event("Decision before generation")
        captured_size = self.transcript.stat().st_size
        self.env["COMPACT_PLUS_PRIMARY_BACKEND"] = (
            'cat > "$TMPDIR/backend-prompt"; '
            "printf '%s\\n' '{\"type\":\"user\",\"message\":{\"content\":\"Later decision\"}}' >> \"$TRANSCRIPT_PATH\"; "
            "printf '## Compact Prep Update\\n- Earlier decision saved.\\n'")
        self.run_hook("precompact-state-summary.sh")
        self.assertEqual(int((self.tmp / "claude-compact-state-offset/macos-test").read_text()), captured_size)
        self.assertNotIn("Later decision", (self.tmp / "backend-prompt").read_text())
        self.capture_backend_prompt()
        self.run_hook("precompact-state-summary.sh")
        prompt = (self.tmp / "backend-prompt").read_text()
        self.assertIn("Later decision", prompt)
        self.assertNotIn("Decision before generation", prompt)

    def test_delta_fallback_appends_without_replacing_base(self):
        self.run_hook("precompact-state-summary.sh")
        self.append_event("Cancel deployment")
        self.capture_backend_prompt()
        self.env["COMPACT_PLUS_FALLBACK_BACKEND"] = self.env["COMPACT_PLUS_PRIMARY_BACKEND"]
        self.env["COMPACT_PLUS_PRIMARY_BACKEND"] = "exit 1"
        self.run_hook("precompact-state-summary.sh")
        state = (self.tmp / "claude-compact-state/macos-test.md").read_text()
        self.assertIn("Test plan", state)
        self.assertIn("## Compact Prep Update", state)

    def test_replaced_transcript_rebuilds_instead_of_appending(self):
        self.run_hook("precompact-state-summary.sh")
        self.transcript.write_text('{"type":"user","message":{"content":"new session context"}}\n')
        self.capture_backend_prompt()
        self.run_hook("precompact-state-summary.sh")
        self.assertNotIn("mode: delta", (self.tmp / "backend-prompt").read_text())
        self.assertIn("Updated plan", (self.tmp / "claude-compact-state/macos-test.md").read_text())

    def test_background_generation_and_recovery(self):
        self.run_hook("stop-compact-plus-pregen.sh")
        state = self.tmp / "claude-compact-state" / "macos-test.md"
        lock = self.tmp / "claude-compact-state-lock" / "macos-test.lock"
        offset = self.tmp / "claude-compact-state-offset" / "macos-test"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if state.exists() and offset.exists() and not lock.exists():
                break
            time.sleep(0.05)
        self.assertTrue(state.exists(), "Detached generation did not save state")
        self.assertFalse(lock.exists())
        self.assertEqual(int(offset.read_text()), self.transcript.stat().st_size)
        counter = self.tmp / "claude-compact-state-counter" / "macos-test"
        self.run_hook("precompact-state-summary.sh")
        self.assertEqual(counter.read_text().strip(), "1", "Fresh state should be reused")
        self.run_hook("compaction-recovery.sh")
        result = json.loads(self.run_hook("userpromptsubmit-compaction-recovery.sh"))
        self.assertIn("Test plan", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_hook("userpromptsubmit-compaction-recovery.sh"), "")


if __name__ == "__main__":
    unittest.main()
