"""Gate new work by budget while owning active responses until completion or cancellation."""

from __future__ import annotations

import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event, Lock

from rca_agent.utils.timeout import call_with_timeout

_CONTROL_POLL_SECONDS = 1.0


class InvocationNotStartedError(TimeoutError):
    """No invocation was admitted; evidence must retain NOT_STARTED and its attempt count."""


class WorkAdmissionError(TimeoutError):
    """An active invocation exhausted its budget for another model/tool request."""


class InvocationStoppedError(RuntimeError):
    """Explicit cancellation or loss of ownership invalidated the invocation's output."""


@dataclass
class _Control:
    deadline: float | None = None
    checks: tuple = ()
    progress: tuple = ()


_control: ContextVar[_Control | None] = ContextVar("rca_control", default=None)
_active: ContextVar[_Invocation | None] = ContextVar("rca_active_invocation", default=None)


@contextmanager
def invocation_scope(*, admission_deadline=None, control=None, on_progress=None):
    """Share a monotonic admission deadline and caller-owned cancellation/heartbeat hooks.

    control() may raise the caller's shutdown/claim-loss exception or return False.
    on_progress(event) receives counters only, never model text. This layer does not
    renew claims or SQS visibility. Nested scopes may shorten but never extend time.
    """
    parent = _control.get() or _Control()
    deadlines = [d for d in (parent.deadline, admission_deadline) if d is not None]
    value = _Control(
        min(deadlines) if deadlines else None,
        (*parent.checks, *((control,) if control is not None else ())),
        (*parent.progress, *((on_progress,) if on_progress is not None else ())),
    )
    token = _control.set(value)
    try:
        yield
    finally:
        _control.reset(token)


@dataclass
class _Invocation:
    deadline: float
    control: _Control
    stopped: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    streams: dict = field(default_factory=dict)
    chunks: int = 0
    last_data: float | None = None
    failure: BaseException | None = None

    def check_control(self):
        """Recheck explicit ownership without interpreting budget expiry as cancellation."""
        if self.failure is not None:
            raise self.failure
        for check in self.control.checks:
            if check() is False:
                raise InvocationStoppedError("invocation cancelled or ownership lost")

    def admit(self):
        """Never interpret the socket idle timeout as a total response lifetime."""
        if self.stopped.is_set():
            raise InvocationStoppedError("invocation no longer owns request admission")
        if time.monotonic() >= self.deadline:
            raise WorkAdmissionError("invocation budget exhausted before new work")

    def notify(self):
        """Let the lease owner observe an active request without receiving model content."""
        event = {
            "event": "invocation_progress",
            "chunks": self.chunks,
            "last_data_monotonic": self.last_data,
            "active_streams": len(self.streams),
        }
        for callback in self.control.progress:
            callback(event)

    def close_streams(self):
        """Request transport shutdown; invocation shutdown still joins the SDK workers."""
        with self.lock:
            streams = list(self.streams.values())
        for stream in streams:
            stream.close()


class _OwnedEventStream:
    """Keep the SDK's first/next socket idle timeout and close the actual response on cancellation."""

    def __init__(self, stream, owner):
        self.stream, self.owner = stream, owner
        self.closed = Event()
        with owner.lock:
            owner.streams[id(self)] = self
        if owner.stopped.is_set():
            self.close()

    def __iter__(self):
        """Deliver an already admitted stream beyond its start budget, never beyond cancellation."""
        try:
            for chunk in self.stream:
                if self.owner.stopped.is_set():
                    raise InvocationStoppedError("model response cancelled")
                self.owner.chunks += 1
                self.owner.last_data = time.monotonic()
                yield chunk
        finally:
            self.close()

    def close(self):
        """Close this invocation's response without closing a shared boto/MCP client."""
        if not self.closed.is_set():
            self.closed.set()
            try:
                self.stream.close()
            finally:
                with self.owner.lock:
                    self.owner.streams.pop(id(self), None)


def guard_model_request(params=None, **kwargs):
    """Check at the actual SDK request boundary, including internal model retries."""
    require_request_budget()


def own_model_response(parsed, **kwargs):
    """Attach only this invocation's streaming response to its cancellation owner."""
    owner = _active.get()
    if owner is not None and isinstance(parsed, dict) and "stream" in parsed:
        parsed["stream"] = _OwnedEventStream(parsed["stream"], owner)


def bounded_admission_deadline(timeout_seconds: float) -> float:
    """Clamp a stage/batch start budget to its inherited overall deadline without resetting either."""
    deadline = time.monotonic() + max(0, timeout_seconds)
    inherited = (_control.get() or _Control()).deadline
    return min(deadline, inherited) if inherited is not None else deadline


def require_request_budget(request_seconds=None):
    """Gate a new model/tool request by start time; request_seconds is not a wallclock reserve."""
    owner = _active.get()
    if owner is not None:
        owner.admit()
    else:
        deadline = (_control.get() or _Control()).deadline
        if deadline is not None and time.monotonic() >= deadline:
            raise WorkAdmissionError("analysis budget exhausted before new work")


class _InvocationExecutor(ThreadPoolExecutor):
    """Own local workers; actual SDK/tool boundaries separately enforce new-work admission."""

    def __init__(self, admission_deadline, owner=None):
        super().__init__(thread_name_prefix="rca-invocation")
        self.admission_deadline, self.owner = admission_deadline, owner
        self.stopped = Event()

    def _check_admission(self):
        if self.stopped.is_set():
            raise InvocationStoppedError("invocation request admission is closed")

    def submit(self, fn, /, *args, **kwargs):
        """Fence cancellation while allowing local result validation after the start budget expires."""
        self._check_admission()

        def run():
            self._check_admission()
            return fn(*args, **kwargs)

        return super().submit(run)

    def stop_admission(self):
        self.stopped.set()


def invoke_agent(agent, prompt: str, output_model, timeout_seconds: float, *, on_started=None):
    """Preserve an admitted response even beyond budget; cancel only on explicit control failure.

    The SDK owns socket idle failures. asyncio shutdown drains the local SDK workers
    before any retry can begin. Remote service cancellation is not inferred from a
    local cancellation notification; invalidated output is never returned to writers.
    """
    inherited = _control.get() or _Control()
    deadline = bounded_admission_deadline(timeout_seconds)
    if time.monotonic() >= deadline:
        raise InvocationNotStartedError("invocation budget exhausted before admission")
    owner = _Invocation(deadline, inherited)
    owner.check_control()
    if not inspect.iscoroutinefunction(getattr(agent, "invoke_async", None)):
        # Non-model synchronous adapters keep their established hard timeout.
        def sync_request():
            if on_started is not None:
                on_started()
            return agent(prompt, structured_output_model=output_model).structured_output

        return call_with_timeout(sync_request, max(0, deadline - time.monotonic()))

    async def run():
        executor = _InvocationExecutor(deadline, owner)
        asyncio.get_running_loop().set_default_executor(executor)
        token = _active.set(owner)
        task = None
        try:
            if time.monotonic() >= deadline:
                raise InvocationNotStartedError("invocation budget exhausted before scheduling")

            async def request():
                if time.monotonic() >= deadline:
                    raise InvocationNotStartedError("invocation budget exhausted before request start")
                if on_started is not None:
                    on_started()
                return await agent.invoke_async(prompt, structured_output_model=output_model)

            task = asyncio.create_task(request())
            while not task.done():
                owner.check_control()
                owner.notify()
                await asyncio.wait({task}, timeout=_CONTROL_POLL_SECONDS)
            result = await task
            owner.check_control()
            return result.structured_output
        except BaseException as exc:
            owner.failure = exc
            owner.stopped.set()
            owner.close_streams()
            raise
        finally:
            executor.stop_admission()
            owner.stopped.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            owner.close_streams()
            _active.reset(token)

    return asyncio.run(run())
