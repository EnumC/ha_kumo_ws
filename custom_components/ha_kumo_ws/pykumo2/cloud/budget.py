"""Cloud call accounting and rate-limit headroom."""

import logging
import re
from collections import Counter, deque
from collections.abc import Callable, Mapping
from typing import Literal, get_args

from ..clock import Clock

_LOGGER = logging.getLogger(__name__)

type CallCategory = Literal[
    "login", "refresh", "get", "command", "relay", "socket_connect", "socket_emit"
]
CATEGORIES: tuple[CallCategory, ...] = get_args(CallCategory.__value__)

WINDOW_S = 3600.0
LOW_HEADROOM = 5
# Values above this are epoch timestamps, below are seconds until reset.
_EPOCH_THRESHOLD = 1_000_000_000

_NUMBER = re.compile(r"\s*(\d+(?:\.\d+)?)")
_PARAM = re.compile(r"\b(limit|remaining|reset|r|t)\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_PARAM_ALIASES = {"r": "remaining", "t": "reset"}


def _header_field(name: str) -> str | None:
    """'x-ratelimit-remaining', 'RateLimit-Remaining', 'x-rate-limit-remaining' -> 'remaining'."""
    key = name.lower().removeprefix("x-").replace("rate-limit", "ratelimit")
    if key == "ratelimit":
        return ""
    suffix = key.removeprefix("ratelimit-")
    return suffix if suffix != key and suffix in ("limit", "remaining", "reset") else None


class CloudCallLedger:
    """Rolling one-hour counts per category plus the server's rate-limit headers."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._events: deque[tuple[float, CallCategory]] = deque()
        self._totals: Counter[CallCategory] = Counter()
        self._limit: int | None = None
        self._remaining: int | None = None
        self._reset_at: float | None = None
        self._remaining_at = 0.0
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Call cb on every change; returns a remover."""
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def record(self, category: CallCategory) -> None:
        self._events.append((self._clock.monotonic(), category))
        self._totals[category] += 1
        self._notify()

    def count_last_hour(self, category: CallCategory | None = None) -> int:
        self._prune()
        return sum(1 for _, cat in self._events if category is None or cat == category)

    def counts_last_hour(self) -> dict[CallCategory, int]:
        self._prune()
        counts = Counter(cat for _, cat in self._events)
        return {cat: counts[cat] for cat in CATEGORIES}

    @property
    def total(self) -> int:
        return sum(self._totals.values())

    def total_for(self, category: CallCategory) -> int:
        return self._totals[category]

    @property
    def limit(self) -> int | None:
        return self._limit

    @property
    def remaining(self) -> int | None:
        """Last reported remaining calls; None once the reported window has reset."""
        if self._reset_at is not None and self._clock.now() >= self._reset_at:
            return None
        if self._reset_at is None and self._clock.monotonic() - self._remaining_at >= WINDOW_S:
            return None
        return self._remaining

    @property
    def reset_at(self) -> float | None:
        """Epoch seconds when the server window resets, if reported."""
        return self._reset_at

    def seconds_until_reset(self) -> float | None:
        if self._reset_at is None:
            return None
        return max(0.0, self._reset_at - self._clock.now())

    def allow(self, essential: bool) -> bool:
        """Refuse non-essential calls when headroom is known and low."""
        remaining = self.remaining
        return essential or remaining is None or remaining > LOW_HEADROOM

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        """Parse x-ratelimit-*, ratelimit-* and combined RateLimit headers."""
        found: dict[str, float] = {}
        for name, value in headers.items():
            fld = _header_field(name)
            if fld is None:
                continue
            if fld:
                match = _NUMBER.match(value)
                if match:
                    found[fld] = float(match.group(1))
                continue
            for key, number in _PARAM.findall(value):
                key = key.lower()
                found.setdefault(_PARAM_ALIASES.get(key, key), float(number))
        if not found:
            return
        if "limit" in found:
            self._limit = int(found["limit"])
        if "remaining" in found:
            self._remaining = int(found["remaining"])
            self._remaining_at = self._clock.monotonic()
        if "reset" in found:
            reset = found["reset"]
            self._reset_at = reset if reset >= _EPOCH_THRESHOLD else self._clock.now() + reset
        self._notify()

    def _prune(self) -> None:
        cutoff = self._clock.monotonic() - WINDOW_S
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                _LOGGER.exception("Ledger listener failed")
