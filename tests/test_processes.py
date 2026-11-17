import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from nvidia_nspawn.common import Error, run
from nvidia_nspawn import runtime


class ProcessTests(unittest.TestCase):
    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_timeout_reaps_ignoring_grandchild_and_closes_output(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            late = Path(directory) / "late"
            code = '''
import os, signal, sys, time
ready, late = sys.argv[1:]
if os.fork() == 0:
    if os.fork() == 0:
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        open(ready, "w").write(str(os.getpid()))
        time.sleep(5)
        open(late, "w").write("escaped")
        os._exit(0)
    os._exit(0)
while not os.path.exists(ready):
    time.sleep(0.01)
time.sleep(60)
'''
            started = time.monotonic()
            with self.assertRaisesRegex(Error, "timed out"):
                run([sys.executable, "-c", code, str(ready), str(late)], timeout=2)
            self.assertLess(time.monotonic() - started, 5)
            self.assert_gone(int(ready.read_text()))
            self.assertFalse(late.exists())

    def test_nonzero_exit_reaps_descendants_holding_output(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            code = '''
import os, signal, sys, time
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
while not os.path.exists(sys.argv[1]):
    time.sleep(0.01)
print("failure details", flush=True)
os._exit(7)
'''
            with self.assertRaisesRegex(Error, "exited 7:.*\\n.*failure details"):
                run([sys.executable, "-c", code, str(ready)], timeout=5)
            self.assert_gone(int(ready.read_text()))

    def test_success_keeps_stdout_and_stderr_modes(self):
        code = 'import sys; print("out", flush=True); print("err", file=sys.stderr)'
        self.assertEqual(run([sys.executable, "-c", code]), "out\nerr\n")
        self.assertEqual(run([sys.executable, "-c", code], merge_stderr=False), "out\n")


    def test_supervision_does_not_kill_unrelated_caller_children(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with self.assertRaises(Error):
                run([sys.executable, "-c", "raise SystemExit(3)"])
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_success_also_reaps_orphaned_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            code = '''
import os, sys, time
if os.fork() == 0:
    os.setsid()
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
while not os.path.exists(sys.argv[1]):
    time.sleep(0.01)
print("done", flush=True)
os._exit(0)
'''
            self.assertEqual(run([sys.executable, "-c", code, str(ready)], timeout=5), "done\n")
            self.assert_gone(int(ready.read_text()))


class ProgressTests(unittest.TestCase):
    def test_recovers_completed_events_and_reports_unknown_tail(self):
        journal, errors = runtime.read_progress(
            b'{"kind":"created","path":"/new"}\n'
            b'{"kind":"mounted","path":"/new/file"}\n'
            b'{"kind":"created","path":')
        self.assertEqual(journal, {"created": ["/new"], "mounted": ["/new/file"]})
        self.assertTrue(errors)

    def test_apply_paths_include_only_planned_targets_and_their_parents(self):
        self.assertEqual(runtime.apply_paths(
            [{"target": "/new/lib/driver.so"}], [{"link": "/new/bin/driver"}], True), [
                "/etc", "/etc/ld.so.conf.d", runtime.MANAGED_CONF,
                "/new", "/new/bin", "/new/bin/driver", "/new/lib", "/new/lib/driver.so"])

    def test_residual_candidates_report_changed_targets_without_claiming_ownership(self):
        before = {"paths": [
            {"path": "/existing", "exists": True, "inode": 1},
            {"path": "/new", "exists": False},
            {"path": "/mount", "exists": True, "inode": 2},
        ], "mounts": [{"id": "10", "target": "/"}]}
        current = {"paths": [
            {"path": "/existing", "exists": True, "inode": 1},
            {"path": "/new", "exists": True, "inode": 3},
            {"path": "/mount", "exists": True, "inode": 2},
        ], "mounts": [{"id": "10", "target": "/"},
                     {"id": "11", "target": "/mount"}]}
        candidates = runtime.residual_candidates(
            before, current, ["/existing", "/new", "/mount"])
        self.assertEqual([entry["path"] for entry in candidates], ["/new", "/mount"])
        self.assertFalse(candidates[0]["new_mount"])
        self.assertTrue(candidates[1]["new_mount"])
        self.assertEqual(candidates[0]["before"], before["paths"][1])
        self.assertEqual(candidates[0]["current"], current["paths"][1])

    def test_unknown_event_is_reported_without_losing_later_events(self):
        journal, errors = runtime.read_progress(
            b'{"kind":"unknown","path":"/uncertain"}\n'
            b'{"kind":"created","path":"/known"}\n')
        self.assertEqual(journal, {"created": ["/known"], "mounted": []})
        self.assertIn("/uncertain", errors[0])

    def test_apply_failure_recovers_fd_events_after_descendants_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "worker.py"
            script.write_text('''
import json, os, signal, sys, time
request = json.load(sys.stdin)
fd = request["journal_fd"]
os.write(fd, b'{"kind":"created","path":"/new-dir"}\\n')
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(fd, b'{"kind":"mounted","path":"/new-dir/file"}\\n')
    time.sleep(60)
    os._exit(0)
time.sleep(60)
''')
            with patch.object(runtime, "WORKER", str(script)):
                with self.assertRaises(runtime.WorkerError) as caught:
                    runtime.apply_worker({}, {}, timeout=2)
            self.assertEqual(caught.exception.journal,
                             {"created": ["/new-dir"], "mounted": ["/new-dir/file"]})
            self.assertTrue(caught.exception.journal_errors)

    def test_complete_worker_error_does_not_replay_rolled_back_events(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "worker.py"
            response = {"error": "apply failed", "cleanup_errors": [],
                        "journal": {"mounted": [], "created": ["/already-removed"]}}
            script.write_text('import json, os, sys\n'
                              'request = json.load(sys.stdin)\n'
                              'os.write(request["journal_fd"], '
                              'b\'{"kind":"created","path":"/already-removed"}\\n\')\n'
                              f'print({json.dumps(json.dumps(response))})\n')
            with patch.object(runtime, "WORKER", str(script)):
                self.assertEqual(json.loads(runtime.apply_worker({}, {}, timeout=5)), response)

    def test_nonzero_apply_recovers_completed_events(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "worker.py"
            script.write_text('import json, os, sys\n'
                              'request = json.load(sys.stdin)\n'
                              'os.write(request["journal_fd"], '
                              'b\'{"kind":"created","path":"/new-dir"}\\n\')\n'
                              'raise SystemExit(4)\n')
            with patch.object(runtime, "WORKER", str(script)):
                with self.assertRaises(runtime.WorkerError) as caught:
                    runtime.apply_worker({}, {
                        "resources": [{"target": "/new-dir/file"}],
                        "links": [], "ldconfig_folders": ["/new-dir"],
                    }, timeout=5)
            self.assertEqual(caught.exception.journal,
                             {"created": ["/new-dir"], "mounted": []})
            self.assertIn("/new-dir", caught.exception.possible_residuals)
            self.assertIn(runtime.MANAGED_CONF, caught.exception.possible_residuals)
            self.assertTrue(caught.exception.journal_errors)



if __name__ == "__main__":
    unittest.main()
