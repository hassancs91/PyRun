"""
The ``pyrunner_run`` script helper on its own, with no Django (2.5, T02).

Every test here is a plain ``unittest.TestCase`` against a stdlib HTTP stub
standing in for PyRun's internal API, so the module runs without Django:

    python -m unittest core.test_pyrunner_run_helper

What it pins: inputs() and run_id() read what the executor hands a run;
set_result() checks its argument before sending, and never loses a refused
result silently; progress() is throttled, merges the calls it holds back,
sends the last one (by timer or at exit) and never raises for delivery.
The server side (masking, the token-bound run, finished runs) is in
core/test_run_reports.py.
"""

import http.client
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

HELPERS = Path(__file__).resolve().parent / "script_helpers"
sys.path.insert(0, str(HELPERS))

import pyrunner_run  # noqa: E402

ENV_KEYS = (
    "PYRUNNER_RUN_FILE",
    "PYRUNNER_RUN_ID",
    "PYRUNNER_INTERNAL_URL",
    "PYRUNNER_INTERNAL_TOKEN",
)


class _Stub(http.server.ThreadingHTTPServer):
    """Records each POST; answers with ``replies[path]`` or 200 {"ok": true}.

    A reply given as bytes is sent as is (to stand in for something that isn't PyRun).
    """

    daemon_threads = True

    def __init__(self):
        self.requests = []
        self.replies = {}
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body)}
        )
        status, reply = self.server.replies.get(self.path, (200, {"ok": True}))
        data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class _HelperTest(unittest.TestCase):
    def setUp(self):
        saved = {k: os.environ.get(k) for k in ENV_KEYS}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(restore)
        for k in ENV_KEYS:
            os.environ.pop(k, None)

        self.stub = _Stub()
        threading.Thread(target=self.stub.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.stub.shutdown)
        os.environ["PYRUNNER_INTERNAL_URL"] = f"http://127.0.0.1:{self.stub.server_address[1]}/"
        os.environ["PYRUNNER_INTERNAL_TOKEN"] = "signed-token"

        # Fresh throttle state per test; pretend the atexit hook is registered
        # so tests don't leave one behind in the test process.
        if pyrunner_run._progress_timer is not None:
            pyrunner_run._progress_timer.cancel()
        pyrunner_run._progress_timer = None
        pyrunner_run._progress_sent_at = None
        pyrunner_run._progress_pending = None
        pyrunner_run._progress_atexit = True

    def _run_file(self, payload):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        with fh:
            json.dump(payload, fh)
        self.addCleanup(os.unlink, fh.name)
        os.environ["PYRUNNER_RUN_FILE"] = fh.name

    def _wait_for_requests(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while len(self.stub.requests) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.stub.requests


class ContextTests(_HelperTest):
    def test_inputs_returns_every_input_json_ones_too(self):
        self._run_file({"run": {"id": "ab"}, "inputs": {"city": "Zürich", "options": {"a": [1, 2]}}})
        self.assertEqual(pyrunner_run.inputs(), {"city": "Zürich", "options": {"a": [1, 2]}})

    def test_inputs_is_empty_when_the_script_declares_none(self):
        self._run_file({"run": {"id": "ab"}, "inputs": {}})
        self.assertEqual(pyrunner_run.inputs(), {})

    def test_each_call_returns_a_new_dict(self):
        self._run_file({"inputs": {"n": 1}})
        pyrunner_run.inputs()["n"] = 2
        self.assertEqual(pyrunner_run.inputs(), {"n": 1})

    def test_outside_a_run_inputs_and_run_id_raise(self):
        with self.assertRaisesRegex(pyrunner_run.RunError, "PYRUNNER_RUN_FILE"):
            pyrunner_run.inputs()
        with self.assertRaisesRegex(pyrunner_run.RunError, "PYRUNNER_RUN_ID"):
            pyrunner_run.run_id()

    def test_an_unreadable_run_file_raises(self):
        os.environ["PYRUNNER_RUN_FILE"] = str(Path(tempfile.gettempdir()) / "no-such-run-file.json")
        with self.assertRaisesRegex(pyrunner_run.RunError, "Could not read the run file"):
            pyrunner_run.inputs()

    def test_run_id_is_the_env_value(self):
        os.environ["PYRUNNER_RUN_ID"] = "0123456789abcdef0123456789abcdef"
        self.assertEqual(pyrunner_run.run_id(), "0123456789abcdef0123456789abcdef")

    def test_the_helper_imports_without_django(self):
        out = subprocess.run(
            [sys.executable, "-c", "import sys, pyrunner_run; print('django' in sys.modules)"],
            env={**os.environ, "PYTHONPATH": str(HELPERS)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "False")


class SetResultTests(_HelperTest):
    def test_posts_the_result_with_the_run_token(self):
        pyrunner_run.set_result({"rows": 42, "name": "Zürich"})
        (req,) = self.stub.requests
        self.assertEqual(req["path"], "/internal/runs/result")
        self.assertEqual(req["auth"], "Bearer signed-token")
        self.assertEqual(req["body"], {"result": {"rows": 42, "name": "Zürich"}})

    def test_bad_arguments_fail_before_any_request(self):
        with self.assertRaises(TypeError):
            pyrunner_run.set_result(["not", "a", "dict"])
        with self.assertRaisesRegex(ValueError, "JSON"):
            pyrunner_run.set_result({"x": float("nan")})
        with self.assertRaisesRegex(ValueError, "JSON"):
            pyrunner_run.set_result({"x": object()})
        with self.assertRaisesRegex(ValueError, "the limit is"):
            pyrunner_run.set_result({"x": "a" * pyrunner_run.RESULT_MAX_BYTES})
        self.assertEqual(self.stub.requests, [])

    def test_a_refusal_raises_with_the_code(self):
        self.stub.replies["/internal/runs/result"] = (
            409,
            {"error": {"code": "RUN_NOT_RUNNING", "message": "This run has finished."}},
        )
        with self.assertRaises(pyrunner_run.RunError) as ctx:
            pyrunner_run.set_result({"a": 1})
        self.assertEqual(ctx.exception.code, "RUN_NOT_RUNNING")
        self.assertIn("This run has finished.", str(ctx.exception))

    def test_unreachable_pyrun_raises(self):
        refused = urllib.error.URLError(ConnectionRefusedError("connection refused"))
        with mock.patch.object(pyrunner_run.urllib.request, "urlopen", side_effect=refused):
            with self.assertRaisesRegex(pyrunner_run.RunError, "Could not reach PyRun"):
                pyrunner_run.set_result({"a": 1})

    def test_a_broken_connection_raises_run_error(self):
        broken = http.client.IncompleteRead(b"")
        with mock.patch.object(pyrunner_run.urllib.request, "urlopen", side_effect=broken):
            with self.assertRaisesRegex(pyrunner_run.RunError, "Could not reach PyRun"):
                pyrunner_run.set_result({"a": 1})

    def test_an_answer_that_isnt_json_raises_run_error(self):
        self.stub.replies["/internal/runs/result"] = (200, b"<html>not PyRun</html>")
        with self.assertRaisesRegex(pyrunner_run.RunError, "wasn't JSON"):
            pyrunner_run.set_result({"a": 1})

    def test_outside_a_run_raises(self):
        os.environ.pop("PYRUNNER_INTERNAL_TOKEN")
        with self.assertRaisesRegex(pyrunner_run.RunError, "Not inside a PyRun run"):
            pyrunner_run.set_result({"a": 1})


class ProgressTests(_HelperTest):
    def _bodies(self):
        return [r["body"] for r in self.stub.requests]

    def test_bad_arguments_raise_straight_away(self):
        with self.assertRaises(ValueError):
            pyrunner_run.progress()
        with self.assertRaises(TypeError):
            pyrunner_run.progress(True)
        with self.assertRaises(TypeError):
            pyrunner_run.progress("50")
        with self.assertRaises(ValueError):
            pyrunner_run.progress(101)
        with self.assertRaises(ValueError):
            pyrunner_run.progress(float("nan"))
        with self.assertRaises(TypeError):
            pyrunner_run.progress(message=5)
        with self.assertRaises(ValueError):
            pyrunner_run.progress(message="x" * (pyrunner_run.PROGRESS_MESSAGE_MAX_CHARS + 1))
        self.assertEqual(self.stub.requests, [])

    def test_the_first_update_is_sent_at_once_with_only_the_given_fields(self):
        pyrunner_run.progress(10)
        pyrunner_run._progress_sent_at = None  # next call is not throttled
        pyrunner_run.progress(message="Fetching")
        self.assertEqual(self._bodies(), [{"pct": 10}, {"message": "Fetching"}])
        self.assertEqual(self.stub.requests[0]["path"], "/internal/runs/progress")

    def test_updates_within_the_interval_are_merged_and_sent_by_the_timer(self):
        with mock.patch.object(pyrunner_run, "PROGRESS_MIN_INTERVAL", 0.3):
            pyrunner_run.progress(0, "Starting")
            pyrunner_run.progress(5, "Page 1")
            pyrunner_run.progress(message="Page 2")
            self.assertEqual(self._bodies(), [{"pct": 0, "message": "Starting"}])
            self._wait_for_requests(2)
        self.assertEqual(self._bodies()[1], {"pct": 5, "message": "Page 2"})
        time.sleep(0.4)
        self.assertEqual(len(self.stub.requests), 2)  # one timer, one send

    def test_a_held_back_update_is_sent_at_exit(self):
        pyrunner_run.progress(10, "a")
        pyrunner_run.progress(90, "almost")  # held back: within the interval
        pyrunner_run._flush_progress_at_exit()
        self.assertEqual(self._bodies(), [{"pct": 10, "message": "a"}, {"pct": 90, "message": "almost"}])
        self.assertIsNone(pyrunner_run._progress_timer)

    def test_the_exit_flush_is_registered_once(self):
        pyrunner_run._progress_atexit = False
        with mock.patch.object(pyrunner_run.atexit, "register") as register:
            pyrunner_run.progress(1)
            pyrunner_run._progress_sent_at = None
            pyrunner_run.progress(2)
        register.assert_called_once_with(pyrunner_run._flush_progress_at_exit)

    def test_a_failed_delivery_warns_on_stderr_and_does_not_raise(self):
        self.stub.replies["/internal/runs/progress"] = (
            409,
            {"error": {"code": "RUN_NOT_RUNNING", "message": "This run has finished."}},
        )
        err = io.StringIO()
        with redirect_stderr(err):
            pyrunner_run.progress(50)
        self.assertIn("pyrunner_run: progress update not saved", err.getvalue())
        self.assertIn("This run has finished.", err.getvalue())

    def test_an_answer_that_isnt_json_only_warns(self):
        self.stub.replies["/internal/runs/progress"] = (200, b"<html>not PyRun</html>")
        err = io.StringIO()
        with redirect_stderr(err):
            pyrunner_run.progress(50)
        self.assertIn("wasn't JSON", err.getvalue())

    def test_outside_a_run_progress_only_warns(self):
        os.environ.pop("PYRUNNER_INTERNAL_TOKEN")
        err = io.StringIO()
        with redirect_stderr(err):
            pyrunner_run.progress(50)
        self.assertIn("Not inside a PyRun run", err.getvalue())


if __name__ == "__main__":
    unittest.main()
