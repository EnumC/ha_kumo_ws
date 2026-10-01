"""Optimistic holds and write generations that mask stale reads after a command."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class _Hold:
    value: Any
    expires_at: float
    token: int = 0
    confirmed: bool = False
    conflict: bool = False
    latest: Any = None


@dataclass(frozen=True, slots=True)
class _Write:
    token: int
    value: Any
    prev: _Write | None = None
    prev_hold: _Hold | None = None


class HoldTable:
    """Per-serial held values; time is passed in so the table stays pure."""

    def __init__(self) -> None:
        self._holds: dict[str, dict[str, _Hold]] = {}
        self._writes: dict[str, dict[str, _Write]] = {}
        self._seq = 0
        self._revoked: set[int] = set()

    def begin_read(self) -> int:
        """Generation for a read starting now; pass it to filter as ``since``."""
        return self._seq

    def add(
        self,
        serial: str,
        values: Mapping[str, Any],
        ttl: float,
        now: float,
        *,
        in_flight: bool = False,
    ) -> int:
        """Hold values for ttl, or indefinitely during delivery. Returns a token."""
        self._seq += 1
        token = self._seq
        held = self._holds.setdefault(serial, {})
        writes = self._writes.setdefault(serial, {})
        for key, value in values.items():
            old_hold = held.get(key)
            held[key] = _Hold(value, float("inf") if in_flight else now + ttl, token)
            writes[key] = _Write(token, value, writes.get(key), old_hold)
        return token

    def extend(self, serial: str, token: int, ttl: float, now: float) -> None:
        """Restart the TTL of holds still owned by token (e.g. once the write finished)."""
        held = self._holds.get(serial, {})
        for key, hold in list(held.items()):
            if hold.token == token:
                held[key] = replace(hold, expires_at=now + ttl)
                writes = self._writes.get(serial, {})
                if key in writes and writes[key].token == token:
                    writes[key] = _Write(token, hold.value)

    def filter(
        self, serial: str, values: Mapping[str, Any], now: float, since: int | None = None
    ) -> dict[str, Any]:
        """Drop values that contradict an active hold or a write newer than ``since``."""
        writes = self._writes.get(serial, {})
        held = self._holds.get(serial)
        result = self.expire(serial, now)
        for key, value in values.items():
            write = writes.get(key)
            if (
                since is not None
                and write is not None
                and write.token > since
                and value != write.value
            ):
                continue
            hold = held.get(key) if held else None
            if hold is None:
                result[key] = value
            elif value == hold.value:
                assert held is not None
                held[key] = replace(hold, confirmed=True)
                result[key] = value
            elif hold.confirmed:
                assert held is not None
                held[key] = replace(hold, conflict=True, latest=value)
        if held is not None and not held:
            del self._holds[serial]
        return result

    def conflict_deadline(self, serial: str) -> float | None:
        deadlines = [h.expires_at for h in self._holds.get(serial, {}).values() if h.conflict]
        return min(deadlines) if deadlines else None

    def expire(self, serial: str, now: float) -> dict[str, Any]:
        """Release expired holds and return their latest confirmed conflicts."""
        held = self._holds.get(serial, {})
        values = {}
        for key, hold in list(held.items()):
            if hold.expires_at <= now:
                if hold.conflict:
                    values[key] = hold.latest
                del held[key]
        return values

    def active(self, serial: str, now: float) -> dict[str, Any]:
        """Currently held values for serial."""
        held = self._holds.get(serial, {})
        return {k: h.value for k, h in held.items() if h.expires_at > now}

    def rollback(self, serial: str, keys: Iterable[str]) -> None:
        """Release holds for keys, e.g. after a failed command."""
        held = self._holds.get(serial)
        if held is None:
            return
        for key in keys:
            held.pop(key, None)
        if not held:
            del self._holds[serial]

    def rollback_token(
        self, serial: str, token: int, keys: Iterable[str] | None = None, now: float = 0.0
    ) -> set[str]:
        """Undo a failed command: release only holds and writes it still owns."""
        if keys is None:
            self._revoked.add(token)
        selected = None if keys is None else set(keys)
        released: set[str] = set()
        held = self._holds.get(serial)
        if held is not None:
            for key in [
                k
                for k, h in held.items()
                if h.token == token and (selected is None or k in selected)
            ]:
                previous = self._writes.get(serial, {}).get(key)
                old_hold = previous.prev_hold if previous is not None else None
                if (
                    old_hold is not None
                    and old_hold.expires_at > now
                    and old_hold.token not in self._revoked
                ):
                    held[key] = old_hold
                else:
                    del held[key]
                released.add(key)
            if not held:
                del self._holds[serial]
        writes = self._writes.get(serial, {})
        for key in [
            k for k, w in writes.items() if w.token == token and (selected is None or k in selected)
        ]:
            prev = writes[key].prev
            while prev is not None and prev.token in self._revoked:
                prev = prev.prev
            if prev is None:
                del writes[key]
            else:
                writes[key] = prev
        return released

    def clear(self, serial: str) -> None:
        self._holds.pop(serial, None)
        self._writes.pop(serial, None)
