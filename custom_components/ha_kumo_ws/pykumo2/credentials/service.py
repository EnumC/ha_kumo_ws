"""Credential store with merge rules, verification and rate-limited refresh."""

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from ..clock import Clock
from ..errors import AuthenticationError, KumoError
from . import codec
from .models import UnitCredentials
from .provider import CloudCredentialProvider
from .repository import CredentialRepository, StoredCredentials

_LOGGER = logging.getLogger(__name__)


class Prober(Protocol):
    """Signed status read against a unit at its known address."""

    async def probe(self, creds: UnitCredentials) -> bool: ...


@dataclass(slots=True)
class MergeReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    kept_old_pair: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RefreshResult:
    refreshed: list[str] = field(default_factory=list)
    skipped_cooldown: list[str] = field(default_factory=list)
    missing: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def _pair(unit: UnitCredentials) -> tuple[str, str]:
    return unit.password_b64.reveal(), unit.crypto_serial_hex.reveal()


class CredentialService:
    """Owns the credential set for one config entry."""

    def __init__(
        self,
        repo: CredentialRepository,
        *,
        provider: CloudCredentialProvider | None,
        prober: Prober | None,
        clock: Clock,
        per_unit_cooldown: float = 6 * 3600,
        global_cooldown: float = 1800,
    ) -> None:
        self._repo = repo
        self._provider = provider
        self._prober = prober
        self._clock = clock
        self._per_unit_cooldown = per_unit_cooldown
        self._global_cooldown = global_cooldown
        self._data = StoredCredentials()
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[set[str]], None]] = []
        self._unit_attempt: dict[str, float] = {}
        self._global_attempt: float | None = None

    async def async_load(self) -> None:
        """Load stored credentials."""
        async with self._lock:
            self._data = await self._repo.async_load()

    def get(self, serial: str) -> UnitCredentials | None:
        return self._data.units.get(serial)

    def all(self) -> Mapping[str, UnitCredentials]:
        return dict(self._data.units)

    def meta(self, serial: str) -> dict[str, Any]:
        return dict(self._data.meta.get(serial, {}))

    def meta_serials(self) -> list[str]:
        """Serials with stored metadata, including ones without credentials."""
        return [serial for serial, values in self._data.meta.items() if values]

    async def async_set_meta(self, serial: str, **values: Any) -> None:
        """Merge non-secret metadata for a serial and persist."""
        self._data.meta.setdefault(serial, {}).update(values)
        await self._repo.async_save(self._data)

    def add_listener(self, cb: Callable[[set[str]], None]) -> Callable[[], None]:
        """Register a callback for changed serials. Returns an unsubscribe function."""
        self._listeners.append(cb)

        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return remove

    def _notify(self, serials: set[str]) -> None:
        if not serials:
            return
        for cb in list(self._listeners):
            try:
                cb(set(serials))
            except Exception:
                _LOGGER.exception("credential listener failed")

    async def _commit(self, changed: set[str]) -> None:
        if changed:
            await self._repo.async_save(self._data)
            self._notify(changed)

    async def _probe(self, creds: UnitCredentials) -> bool:
        if self._prober is None:
            return False
        try:
            return await self._prober.probe(creds)
        except Exception:
            return False

    async def async_merge(
        self, incoming: Iterable[UnitCredentials], *, verified: bool = False
    ) -> MergeReport:
        """Merge units into the store following the precedence rules."""
        report = MergeReport()
        async with self._lock:
            changed: set[str] = set()
            for unit in incoming:
                merged = await self._merge_one(unit, verified, report)
                if merged is not None:
                    self._data.units[unit.serial] = merged
                    changed.add(unit.serial)
            await self._commit(changed)
        return report

    async def _merge_one(
        self, new: UnitCredentials, verified: bool, report: MergeReport
    ) -> UnitCredentials | None:
        serial = new.serial
        old = self._data.units.get(serial)
        if new.has_secrets:
            try:
                new.validate()
            except ValueError:
                report.rejected[serial] = "invalid_credentials"
                return None
        elif old is None or not old.has_secrets:
            report.rejected[serial] = "missing_secrets"
            return None
        now = self._clock.now()
        base = old if old is not None and old.has_secrets else None
        use_new_pair = new.has_secrets
        candidate = self._combine(base, new, now)
        kept_old = False
        if base is None:
            candidate = replace(candidate, verified_at=now if verified else new.verified_at)
        elif use_new_pair and _pair(new) != _pair(base):
            if verified:
                candidate = replace(candidate, verified_at=now)
            elif self._prober is not None and candidate.address:
                if await self._probe(candidate):
                    candidate = replace(candidate, verified_at=now)
                elif await self._probe(base):
                    candidate = replace(
                        candidate,
                        password_b64=base.password_b64,
                        crypto_serial_hex=base.crypto_serial_hex,
                        source=base.source,
                        verified_at=base.verified_at,
                    )
                    kept_old = True
                elif base.verified_at is not None:
                    report.rejected[serial] = "unverified_candidate"
                    return None
                else:
                    candidate = replace(candidate, verified_at=None)
            elif base.verified_at is not None:
                report.rejected[serial] = "unverified_candidate"
                return None
            else:
                candidate = replace(candidate, verified_at=None)
        elif verified:
            candidate = replace(candidate, verified_at=now)
        if kept_old:
            report.kept_old_pair.append(serial)
        if old is not None and replace(candidate, updated_at=old.updated_at) == old:
            report.unchanged.append(serial)
            return None
        candidate = replace(candidate, updated_at=now)
        (report.updated if old is not None else report.added).append(serial)
        return candidate

    @staticmethod
    def _combine(base: UnitCredentials | None, new: UnitCredentials, now: float) -> UnitCredentials:
        """Field-wise merge. Verified_at and source follow the secret pair."""
        if base is None:
            return replace(new, updated_at=now)
        if new.has_secrets and _pair(new) != _pair(base):
            pair_src, verified_at, source = new, None, new.source
        else:
            pair_src, verified_at, source = base, base.verified_at, base.source
        if base.address_pinned:
            address, pinned = base.address, True
        elif new.address and not (
            base.address and base.verified_at is not None and not new.address_pinned
        ):
            address, pinned = new.address, new.address_pinned
        else:
            address, pinned = base.address, False
        unit_type = base.unit_type if new.unit_type in ("", "ductless") else new.unit_type
        return UnitCredentials(
            serial=new.serial,
            password_b64=pair_src.password_b64,
            crypto_serial_hex=pair_src.crypto_serial_hex,
            label=new.label or base.label,
            mac=new.mac or base.mac,
            unit_type=unit_type,
            address=address,
            address_pinned=pinned,
            source=source,
            updated_at=now,
            verified_at=verified_at,
        )

    async def _update(
        self,
        serial: str,
        *,
        unless: Callable[[UnitCredentials], bool] | None = None,
        **values: Any,
    ) -> None:
        async with self._lock:
            unit = self._data.units.get(serial)
            if unit is None or (unless is not None and unless(unit)):
                return
            updated = replace(unit, **values)
            if updated == unit:
                return
            self._data.units[serial] = updated
            await self._commit({serial})

    async def async_set_address(self, serial: str, address: str, *, pinned: bool = False) -> None:
        """Set a verified/DHCP address, or a pinned override. Pinned wins over unpinned."""
        if not address:
            return
        await self._update(
            serial,
            unless=lambda unit: unit.address_pinned and not pinned,
            address=address,
            address_pinned=pinned,
        )

    async def async_clear_pin(self, serial: str) -> None:
        await self._update(serial, address_pinned=False)

    async def async_mark_verified(self, serial: str) -> None:
        await self._update(serial, verified_at=self._clock.now())

    async def async_request_refresh(
        self, serials: Sequence[str], *, reason: str, force: bool = False
    ) -> RefreshResult:
        """Refresh credentials from the cloud, honoring cooldowns unless forced."""
        result = RefreshResult()
        if self._provider is None:
            result.error = "no_cloud_login"
            return result
        now = self._clock.monotonic()
        if not force:
            if (
                self._global_attempt is not None
                and now - self._global_attempt < self._global_cooldown
            ):
                result.skipped_cooldown = list(serials)
                return result
            ready = []
            for serial in serials:
                last = self._unit_attempt.get(serial)
                if last is not None and now - last < self._per_unit_cooldown:
                    result.skipped_cooldown.append(serial)
                else:
                    ready.append(serial)
        else:
            ready = list(serials)
        if not ready:
            return result
        _LOGGER.debug("credential refresh (%s) for %s", reason, ready)
        self._global_attempt = now
        for serial in ready:
            self._unit_attempt[serial] = now
        try:
            fetched = await self._provider.fetch(ready)
        except AuthenticationError:
            result.error = "auth_failed"
            return result
        except KumoError:
            result.error = "cloud_error"
            return result
        result.missing = dict(fetched.missing)
        report = await self.async_merge(fetched.units)
        result.refreshed = [u.serial for u in fetched.units if u.serial not in report.rejected]
        return result

    def export_backup(self) -> str:
        return codec.encode_backup(self._data.units.values())

    def export_kumo_cache(self) -> str:
        return codec.encode_kumo_cache(self._data.units.values())

    async def async_import(self, text: str) -> MergeReport:
        """Import a backup, kumo_cache or zone table document."""
        try:
            decoded = codec.decode(text)
        except ValueError as exc:
            return MergeReport(rejected={"import": str(exc)})
        report = await self.async_merge(decoded.units)
        for error in decoded.errors:
            key, sep, reason = error.partition(": ")
            if sep:
                report.rejected[key] = reason
            else:
                report.rejected["import"] = error
        return report
