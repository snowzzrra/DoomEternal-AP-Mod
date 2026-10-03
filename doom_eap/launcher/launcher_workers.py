"""Application worker lifetime, serialization and connection cancellation."""
from dataclasses import dataclass, field
from collections.abc import Callable, Hashable
import threading
import time


class LauncherWorkCancelled(Exception):
    """A retired application operation must stop at its next adapter boundary."""


@dataclass(frozen=True)
class LauncherJob:
    key: Hashable
    generation: int
    _cancelled: threading.Event = field(repr=False, compare=False)
    _publication_lock: object = field(default_factory=threading.Lock, repr=False, compare=False)

    def check(self) -> None:
        if self._cancelled.is_set():
            raise LauncherWorkCancelled()

    def event(self, payload: dict[str, object]) -> dict[str, object]:
        self.check()
        return {**payload, "launcher_job_generation": self.generation}

    def publish(self, operation: Callable):
        """Commit a bounded file publication before retirement can be accepted."""
        with self._publication_lock:
            self.check()
            return operation()


class LauncherWorkers:
    """One serialized adapter lane; retained threads never outlive ownership silently.

    Cancellation is cooperative. An in-progress OS call can finish before its next
    boundary, but replacement work cannot overtake it in the same installation.
    """

    def __init__(self, error_sink: Callable[[LauncherJob, Exception], None]):
        self._error_sink = error_sink
        self._lock = threading.Lock()
        self._serial = threading.Lock()
        self._publication_lock = threading.Lock()
        self._generation = 0
        self._closed = False
        self._jobs: dict[Hashable, LauncherJob] = {}
        self._threads: set[threading.Thread] = set()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def active(self) -> frozenset[Hashable]:
        with self._lock:
            return frozenset(self._jobs)

    def accepts(self, generation: int) -> bool:
        with self._lock:
            return not self._closed and generation == self._generation

    def invalidate(self) -> None:
        with self._publication_lock, self._lock:
            self._generation += 1
            for job in self._jobs.values():
                job._cancelled.set()

    def submit(self, key: Hashable, operation: Callable[[LauncherJob], None], *, generation: int | None = None) -> bool:
        with self._lock:
            prior = self._jobs.get(key)
            if self._closed or (generation is not None and generation != self._generation):
                return False
            if prior is not None and not prior._cancelled.is_set():
                return False
            job = LauncherJob(key, self._generation, threading.Event(), self._publication_lock)

            def run() -> None:
                try:
                    with self._serial:
                        job.check()
                        operation(job)
                except LauncherWorkCancelled:
                    pass
                except Exception as error:
                    if self.accepts(job.generation):
                        self._error_sink(job, error)
                finally:
                    with self._lock:
                        if self._jobs.get(key) is job:
                            self._jobs.pop(key)
                        self._threads.discard(threading.current_thread())

            thread = threading.Thread(target=run, name="DoomLauncherWorker", daemon=True)
            self._jobs[key] = job
            self._threads.add(thread)
            thread.start()
            return True

    def close(self, timeout: float = 1.0) -> None:
        with self._publication_lock, self._lock:
            self._closed = True
            self._generation += 1
            for job in self._jobs.values():
                job._cancelled.set()
            threads = tuple(self._threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
