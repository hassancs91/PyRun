"""
RunBackend interface + the data passed across the seam.

Deliberately dependency-light: no imports from ``core.models``/services/executor,
so a backend module can be reasoned about (and unit-tested) in isolation.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

# The outcome of stopping an execution (``RunResult.stop``).
STOP_GRACEFUL = "graceful"  # it exited within the grace period after the stop signal
STOP_KILLED = "killed"  # it was still running when the grace period ran out


@dataclass
class RunSpec:
    """Everything core prepares for one execution; backend-agnostic."""

    cmd: list
    env: dict
    cwd: str
    timeout: Optional[int] = None
    # Optional resource caps (posix only):
    # {"memory_bytes", "cpu_seconds", "nproc", "fsize_bytes"}.
    # None = off (the default), reproducing today's behavior.
    limits: Optional[dict] = None
    # Graceful stop (2.5): on a timeout, or once ``stop_requested()`` returns
    # True, the backend sends the stop signal (SIGTERM / CTRL_BREAK), waits
    # ``stop_grace`` seconds, then kills the tree. 0 = kill at once (the
    # pre-2.5 behavior). ``stop_requested`` is polled while the command runs;
    # it must not raise. None = nobody can ask this execution to stop.
    stop_grace: int = 0
    stop_requested: Optional[Callable[[], bool]] = None


@dataclass
class RunResult:
    """What a backend reports back; core maps it to the Run row.

    ``stdout``/``stderr`` are returned RAW (unmasked, untruncated) — masking and
    truncation stay in core so the resolved-secret set lives in exactly one place.
    On a timeout, core forces ``exit_code = -1`` regardless of what is reported.
    ``stop`` says how a stopped execution ended (``STOP_GRACEFUL`` or
    ``STOP_KILLED``): set on a timeout and when ``stop_requested`` fired, ""
    when the command ended on its own.
    """

    exit_code: Optional[int]
    stdout: Optional[str]
    stderr: Optional[str]
    timed_out: bool = False
    stop: str = ""


@dataclass
class RunHandle:
    """An opaque handle to a started execution.

    ``pid`` is what core records on the Run for force-stop (local backend).
    ``native`` carries the backend's private object (the ``Popen`` for local; a
    container id for a future backend) and is never read by core. ``spec`` is
    the spec it was started from (the local backend reads the stop settings
    from it).
    """

    pid: Optional[int]
    native: Any = None
    spec: Optional[RunSpec] = None


class RunBackend(ABC):
    """Spawn a prepared command, wait for it (enforcing the timeout + killing the
    tree internally), and kill it on demand. Stateless w.r.t. the Run lifecycle."""

    @abstractmethod
    def start(self, spec: RunSpec) -> RunHandle:
        """Launch the command and return a handle (records ``pid`` for local)."""

    @abstractmethod
    def wait(self, handle: RunHandle, timeout: Optional[int]) -> RunResult:
        """Wait up to ``timeout`` seconds; on timeout, stop the tree (gracefully
        when the spec gives a grace period), drain output, and return
        ``timed_out=True``."""

    @abstractmethod
    def kill(self, handle: RunHandle) -> None:
        """Force-kill the execution's process tree (for external force-stop)."""

    def signal_stop(self, handle: RunHandle) -> None:
        """Ask the execution to shut down (the graceful stop signal).

        A backend that has no graceful signal kills instead.
        """
        self.kill(handle)
