"""Circuit breakers for the query path's dependencies (SPEC: Query path, "Dependencies,
timeouts and fallbacks"; DECISIONS 2026-11-24, "Step 20").

One breaker per dependency: the query embedding, the reranker and the primary LLM. When a
dependency keeps failing, its breaker opens and requests skip it, going straight to the
fallback they'd have reached anyway (lexical-only recall, RRF order, the secondary LLM) with
the same degraded flag. That saves each request the wait for a call that would fail: up to
about 4.5 s for the embedding, 1.5 s for the reranker, 6 s for the primary.

The rules:
  - closed: calls go through. 5 failures within a sliding 30 s window open it. A success
    doesn't reset the count, so a dependency that fails half the time still trips it.
  - open: calls are skipped for 30 s.
  - half_open: then one trial call goes through, and other calls skip until it ends. A
    successful trial closes the breaker and clears the window; a failed one reopens it for
    another 30 s. A trial that ends with neither (cancelled, or a bug raised something
    unexpected) is released without a verdict, so the next call becomes the trial. Without
    that, one cancelled request could leave the dependency skipped for good.

What counts as a failure is the caller's decision, and it's exactly what triggers the
fallback (a citation retry is not a failure: DECISIONS 2026-11-24).

State is per process, so on Lambda per instance, not global (SPEC). A new instance has to
see its own 5 failures before it stops calling a dead dependency; shared state would cost a
round trip on every call. No lock: attempts start and end on the event loop, never on the
worker threads that run blocking boto3 calls, and asyncio runs one thing at a time.
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal

log = logging.getLogger(__name__)

State = Literal["closed", "open", "half_open"]

FAILURE_THRESHOLD = 5
WINDOW_SECONDS = 30.0
OPEN_SECONDS = 30.0


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        window_s: float = WINDOW_SECONDS,
        open_s: float = OPEN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._window_s = window_s
        self._open_s = open_s
        self._clock = clock
        self._failures: deque[float] = deque()  # times of recent failures, while closed
        self._opened_at: float | None = None  # None: closed
        self._trial_running = False

    @property
    def state(self) -> State:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at < self._open_s:
            return "open"
        return "half_open"

    def attempt(self) -> "Attempt":
        """Whether this call may go through. Use it as a context manager, so a half-open
        trial is always released (see the module docstring)."""
        state = self.state
        if state == "closed":
            return Attempt(self, state, allowed=True, trial=False)
        if state == "open" or self._trial_running:
            return Attempt(self, state, allowed=False, trial=False)
        self._trial_running = True
        return Attempt(self, state, allowed=True, trial=True)

    def _record_failure(self, trial: bool) -> None:
        now = self._clock()
        if trial:
            self._trial_running = False
            self._opened_at = now
            log.warning("breaker %s: trial call failed, open for %.0f s", self.name, self._open_s)
            return
        if self._opened_at is not None:
            # A call let through before the breaker opened, ending late. The breaker is
            # already open; only the trial's result decides what happens next.
            return
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self._window_s:
            self._failures.popleft()
        if len(self._failures) >= self._threshold:
            self._opened_at = now
            self._failures.clear()
            log.warning(
                "breaker %s: %d failures in %.0f s, open for %.0f s",
                self.name,
                self._threshold,
                self._window_s,
                self._open_s,
            )

    def _record_success(self, trial: bool) -> None:
        # Only the trial's success closes the breaker. A success while closed leaves the
        # failure window as it is (no reset), and a late one while open changes nothing.
        if trial:
            self._trial_running = False
            self._opened_at = None
            self._failures.clear()
            log.info("breaker %s: trial call succeeded, closed", self.name)

    def _release(self) -> None:
        """A trial ended with no verdict: the next call becomes the trial."""
        self._trial_running = False


class Attempt:
    """One call's permission. `state` is the breaker's state when it was decided, for the
    dependency's span (breaker.state)."""

    def __init__(self, breaker: CircuitBreaker, state: State, *, allowed: bool, trial: bool):
        self.state = state
        self.allowed = allowed
        self._breaker = breaker
        self._trial = trial
        self._done = False

    def succeeded(self) -> None:
        self._finish()
        self._breaker._record_success(self._trial)

    def failed(self) -> None:
        self._finish()
        self._breaker._record_failure(self._trial)

    def _finish(self) -> None:
        if not self.allowed:
            raise RuntimeError("a skipped call has no outcome to record")
        if self._done:
            raise RuntimeError("this call's outcome is already recorded")
        self._done = True

    def __enter__(self) -> "Attempt":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._trial and not self._done:
            self._breaker._release()


@dataclass
class Breakers:
    """The API's breakers, one set per process (api/app.py puts it on app.state). Shared by
    /v1/retrieve and /v1/query: a breaker belongs to a dependency, not to an endpoint. The
    secondary LLM has none: its fallback is already an abstain (DECISIONS, "Step 20")."""

    embedding: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("embedding"))
    reranker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("reranker"))
    llm_primary: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("llm_primary"))
