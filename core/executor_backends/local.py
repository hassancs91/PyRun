"""
LocalSubprocessBackend — the executor's spawn/wait/stop/kill.

This is the default backend (``PYRUNNER_RUN_BACKEND`` unset). The script is
launched in its own process group/session (win/posix isolation flags) so the
web process can later stop or kill the whole job tree (force stop / timeout)
without touching the django-q worker that spawned it. A timeout, or a stop
request (``RunSpec.stop_requested``), first sends the stop signal and waits
``RunSpec.stop_grace`` seconds (2.5); with no grace period it is the pre-2.5
kill-the-tree-then-drain.
"""

import logging
import os
import signal
import subprocess
import sys
import time

from core.executor_backends.base import (
    STOP_GRACEFUL,
    STOP_KILLED,
    RunBackend,
    RunHandle,
    RunResult,
    RunSpec,
)

logger = logging.getLogger(__name__)

# How often ``wait`` asks ``RunSpec.stop_requested`` whether someone stopped
# the run. The stop signal itself is sent straight away by whoever stopped it;
# this only decides how soon the grace-period countdown starts here.
STOP_POLL_SECONDS = 2

# Sends CTRL_BREAK_EVENT to a script's process group on Windows. A script runs
# with its own (hidden) console, and GenerateConsoleCtrlEvent only reaches
# processes on the caller's console, so this helper leaves its own console,
# attaches to the script's and sends the event there. It never runs on the
# worker's console: an unknown group id there would send the event to every
# process on it, the worker included. Exit 2: no such console (it ended).
_CTRL_BREAK_HELPER = (
    "import ctypes, sys\n"
    "k = ctypes.windll.kernel32\n"
    "pid = int(sys.argv[1])\n"
    "k.FreeConsole()\n"
    "if not k.AttachConsole(pid):\n"
    "    sys.exit(2)\n"
    "k.SetConsoleCtrlHandler(None, True)\n"
    "sys.exit(0 if k.GenerateConsoleCtrlEvent(1, pid) else 3)\n"
)


def _script_group(pid: int):
    """The process group of a script started by this backend, or None.

    A script leads its own session (``start_new_session=True``), so its group
    id equals its pid. A pid whose group doesn't match was reused by some other
    process, and the caller's own group is never ours to signal: either way,
    nothing is sent. That is what keeps the django-q worker out of reach.
    """
    pgid = os.getpgid(pid)
    if pgid != pid or pgid == os.getpgrp():
        logger.warning("Not signalling pid %s: it doesn't lead its own process group", pid)
        return None
    return pgid


def kill_process_tree(pid: int) -> None:
    """
    Kill a script job's entire process tree.

    This targets *only* the child process the executor spawned (the user's
    Python script and anything it spawned) — never the long-lived django-q
    worker. The child is launched in its own process group/session (see
    ``LocalSubprocessBackend.start``) so the kill is isolated from the worker.

    Args:
        pid: PID of the spawned script subprocess (the process-group leader).
    """
    if not pid:
        return
    try:
        if os.name == "nt":
            # /T kills the whole tree (children too), /F forces it.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            # Child was started with start_new_session=True, so it leads its own
            # process group; killing the group takes out the script + children.
            pgid = _script_group(pid)
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError) as e:
        # Process already gone (or PID reused/invalid) — nothing to do.
        logger.warning(f"Could not kill process tree {pid}: {e}")


def signal_process_tree(pid: int) -> bool:
    """Ask a script job's process tree to shut down: the graceful stop signal.

    SIGTERM to the script's process group (posix), CTRL_BREAK_EVENT to it on
    Windows. Like ``kill_process_tree`` it targets only the job's own group,
    never the django-q worker. Returns True when the signal was sent; False
    when the process is gone (or isn't a script this backend started).
    """
    if not pid or pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            done = subprocess.run(
                [sys.executable, "-c", _CTRL_BREAK_HELPER, str(pid)],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return done.returncode == 0
        pgid = _script_group(pid)
        if pgid is None:
            return False
        os.killpg(pgid, signal.SIGTERM)
        return True
    except (ProcessLookupError, OSError, subprocess.SubprocessError) as e:
        logger.warning(f"Could not send the stop signal to process tree {pid}: {e}")
        return False


def _make_rlimit_preexec(limits: dict):
    """Build a posix ``preexec_fn`` that applies resource caps in the child.

    Runs in the forked child just before ``exec`` (posix only). Each cap is
    applied independently and only when set, so an unconfigured limit leaves the
    inherited (unlimited) value untouched — the sandbox's rlimits floor.
    """

    def _apply():
        import resource

        mem = limits.get("memory_bytes")
        if mem:
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        cpu = limits.get("cpu_seconds")
        if cpu:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        nproc = limits.get("nproc")
        if nproc:
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
        fsize = limits.get("fsize_bytes")
        if fsize:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))

    return _apply


class LocalSubprocessBackend(RunBackend):
    """Run the command as a local subprocess in the PyRun container."""

    def start(self, spec: RunSpec) -> RunHandle:
        # Launch the script in its own process group/session so the web process
        # can later kill the whole job tree without touching the django-q worker.
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": spec.cwd,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": spec.env,
        }
        if os.name == "nt":
            # CREATE_NO_WINDOW: no console popup. CREATE_NEW_PROCESS_GROUP:
            # isolate the child so signals/kills don't reach the worker.
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            # Child becomes the leader of a new session/process group.
            popen_kwargs["start_new_session"] = True
            # Optional resource caps, applied in the child before exec (posix
            # only). Off unless configured, so the default is unchanged.
            if spec.limits:
                popen_kwargs["preexec_fn"] = _make_rlimit_preexec(spec.limits)

        proc = subprocess.Popen(spec.cmd, **popen_kwargs)
        return RunHandle(pid=proc.pid, native=proc, spec=spec)

    def wait(self, handle: RunHandle, timeout) -> RunResult:
        proc = handle.native
        spec = handle.spec
        grace = spec.stop_grace if spec else 0
        stop_requested = spec.stop_requested if spec else None
        # communicate() can be called again after a TimeoutExpired without
        # losing output, so the wait runs in slices and asks between them
        # whether someone stopped the run.
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            step = remaining
            if stop_requested is not None:
                step = STOP_POLL_SECONDS if remaining is None else min(STOP_POLL_SECONDS, remaining)
            try:
                stdout, stderr = proc.communicate(timeout=step)
                return RunResult(
                    exit_code=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=False,
                )
            except subprocess.TimeoutExpired:
                pass
            if deadline is not None and time.monotonic() >= deadline:
                return self._stop(proc, grace, timed_out=True, send_signal=True)
            if stop_requested is not None and stop_requested():
                # Whoever stopped the run already sent the stop signal; the
                # grace period counts from now.
                return self._stop(proc, grace, timed_out=False, send_signal=False)

    @staticmethod
    def _stop(proc, grace, *, timed_out, send_signal) -> RunResult:
        """Stop the job tree, gracefully when there is a grace period.

        With a grace period: send the stop signal (unless it's already been
        sent), give the script ``grace`` seconds to exit, then kill whatever
        is left. Without one: kill the whole tree at once (the pre-2.5
        behaviour). Either way, drain the output buffered so far.
        """
        outcome = STOP_KILLED
        stdout = stderr = None
        if grace > 0:
            if send_signal:
                signal_process_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=grace)
                outcome = STOP_GRACEFUL
            except subprocess.TimeoutExpired:
                pass
        if outcome == STOP_KILLED:
            # Kill the whole job tree (not just the immediate child), then drain
            # whatever output was buffered before the kill.
            kill_process_tree(proc.pid)
            stdout, stderr = proc.communicate()
        return RunResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            stop=outcome,
        )

    def kill(self, handle: RunHandle) -> None:
        kill_process_tree(handle.pid)

    def signal_stop(self, handle: RunHandle) -> None:
        signal_process_tree(handle.pid)
