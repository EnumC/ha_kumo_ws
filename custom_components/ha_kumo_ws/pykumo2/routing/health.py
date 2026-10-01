"""Circuit breaker with half-open probe backoff for one local unit."""

import random
from enum import StrEnum

from ..clock import Clock
from ..errors import LocalAuthError


class CircuitState(StrEnum):
    CLOSED = "closed"
    DEGRADED = "degraded"
    OPEN = "open"
    HALF_OPEN = "half_open"


class HealthTracker:
    """Counts local failures; opens after open_after and schedules jittered probes."""

    def __init__(
        self,
        clock: Clock,
        *,
        degrade_after: int = 1,
        open_after: int = 3,
        probe_initial: float = 60.0,
        probe_max: float = 900.0,
        recover_successes: int = 2,
        jitter: float = 0.1,
        rng: random.Random | None = None,
    ) -> None:
        if not 1 <= degrade_after <= open_after:
            raise ValueError("need 1 <= degrade_after <= open_after")
        self._clock = clock
        self._degrade_after = degrade_after
        self.open_after = open_after
        self._probe_initial = probe_initial
        self._probe_max = probe_max
        self._recover_successes = recover_successes
        self._jitter = jitter
        self._rng = rng or random.Random()
        self.consecutive_failures = 0
        self.consecutive_auth_failures = 0
        self.probe_successes = 0
        self.next_probe_at: float | None = None
        self.last_error: BaseException | None = None
        self._open = False
        self._backoff = probe_initial

    def reset(self) -> None:
        """Back to closed with all counters cleared."""
        self.consecutive_failures = 0
        self.consecutive_auth_failures = 0
        self.probe_successes = 0
        self.next_probe_at = None
        self.last_error = None
        self._open = False
        self._backoff = self._probe_initial

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def probe_due(self) -> bool:
        return (
            self._open
            and self.next_probe_at is not None
            and self._clock.monotonic() >= self.next_probe_at
        )

    @property
    def state(self) -> CircuitState:
        if self._open:
            if self.probe_successes or self.probe_due:
                return CircuitState.HALF_OPEN
            return CircuitState.OPEN
        if self.consecutive_failures >= self._degrade_after:
            return CircuitState.DEGRADED
        return CircuitState.CLOSED

    def record_success(self) -> None:
        """A poll or probe succeeded; closes after recover_successes probes."""
        self.consecutive_failures = 0
        self.consecutive_auth_failures = 0
        self.last_error = None
        if not self._open:
            return
        self.probe_successes += 1
        if self.probe_successes >= self._recover_successes:
            self.reset()
        else:
            self._schedule(self._probe_initial)

    def record_failure(self, error: BaseException | None = None) -> None:
        """A poll, command or probe failed."""
        self.consecutive_failures += 1
        if isinstance(error, LocalAuthError):
            self.consecutive_auth_failures += 1
        else:
            self.consecutive_auth_failures = 0
        self.last_error = error
        if self._open:
            self.probe_successes = 0
            self._backoff = min(self._backoff * 2, self._probe_max)
            self._schedule(self._backoff)
        elif self.consecutive_failures >= self.open_after:
            self.trip()

    def trip(self) -> None:
        """Open the circuit now; no-op if already open."""
        if self._open:
            return
        self._open = True
        self.probe_successes = 0
        self._backoff = self._probe_initial
        self._schedule(self._backoff)

    def request_probe(self) -> None:
        """Make the next probe due immediately."""
        if self._open:
            self.next_probe_at = self._clock.monotonic()

    def _schedule(self, base: float) -> None:
        factor = 1.0 + self._rng.uniform(-self._jitter, self._jitter)
        self.next_probe_at = self._clock.monotonic() + min(base * factor, self._probe_max)
