"""Injectable time source."""

import time
from typing import Protocol


class Clock(Protocol):
    """Monotonic and wall time source."""

    def monotonic(self) -> float: ...

    def now(self) -> float: ...


class SystemClock:
    """Clock backed by the time module."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> float:
        return time.time()
