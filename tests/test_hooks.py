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
            "printf '# Compact Prep State\\n## Active Plan\\nUpdated plan\\n'")

    def test_small_last_minute_change_is_saved(self):
        self.run_hook("precompact-state-summary.sh")
        self.append_event("Cancel the deployment; keep the changes local.")
        self.capture_backend_prompt()
        # Even a configured large threshold must not hide the final decision.
        self.env["COMPACT_PLUS_FRESH_DELTA_KB"] = "999999"
        self.run_hook("precompact-state-summary.sh")
        prompt = (self.tmp / "backend-prompt").read_text()
        self.assertIn("mode: incremental", prompt)
        self.assertIn("Cancel the deployment", prompt)
        self.assertIn("Test plan", prompt)
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
