"""KumoHub: builds and owns the pykumo2 object graph for one config entry."""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util.ssl import SSL_ALPN_HTTP11_HTTP2

from .adapters import (
    CloudInventory,
    CryptoSerialSource,
    PasswordSource,
    SignedProber,
    SubnetScanner,
    ZoneRecord,
    async_default_cidrs,
)
from .cn105_task import Cn105Poller
from .const import (
    CN105_CODES,
    CONF_CIDRS,
    CONF_CN105_CODES,
    CONF_CN105_ENABLED,
    CONF_CN105_INTERVAL,
    CONF_CONNECTION_MODE,
    CONF_IP_OVERRIDES,
    CONF_LOCAL_ROOM_TEMP_OFFSET,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_ON_CONNECT,
    CONF_REMOTE_TEMP,
    CONF_RT_ENTITY,
    CONF_RT_INTERVAL,
    CONF_RT_MANAGE_SOURCE,
    CONF_SETUP_METHOD,
    CONF_SITE_IDS,
    CONF_SOCKET_IDLE_DISCONNECT,
    DEFAULT_CN105_CODES,
    DEFAULT_MODE_BY_METHOD,
    DEFAULT_OPTIONS,
    DEFAULT_RT_INTERVAL,
    DOMAIN,
    INVENTORY_REFRESH_S,
    MAX_POLL_INTERVAL,
    MIN_CN105_INTERVAL,
    MIN_POLL_INTERVAL,
    new_device_signal,
)
from .coordinator import KumoDevice, KumoDeviceCoordinator
from .pykumo2.clock import SystemClock
from .pykumo2.cloud.budget import CloudCallLedger
from .pykumo2.cloud.codec import CloudCodec
from .pykumo2.cloud.rest import CloudRestClient
from .pykumo2.cloud.socket import CloudSocketSession
from .pykumo2.cloud.tokens import TokenManager
from .pykumo2.cloud.transport import CloudTransport
from .pykumo2.credentials.models import UnitCredentials
from .pykumo2.credentials.provider import CloudCredentialProvider
from .pykumo2.credentials.service import CredentialService
from .pykumo2.domain.commands import CommandValidator
from .pykumo2.domain.enums import ConnectionMode, SetupMethod
from .pykumo2.domain.state import StatePatch
from .pykumo2.errors import AuthenticationError, KumoError
from .pykumo2.local.client import LocalUnitClient
from .pykumo2.local.codec import LocalCodec
from .pykumo2.local.transport import LocalTransport
from .pykumo2.routing.address import AddressResolver
from .pykumo2.routing.health import HealthTracker
from .pykumo2.routing.policy import RoutingPolicy
from .pykumo2.routing.router import DeviceLink
from .pykumo2.routing.session_manager import CloudLeaseManager
from .pykumo2.transport import TransportKind
from .remote_temp import RemoteTempFeeder
from .storage import HaCredentialRepository

_LOGGER = logging.getLogger(__name__)

type KumoConfigEntry = ConfigEntry[KumoHub]
type _Inventory = dict[str, tuple[KumoDevice, dict[str, Any]]]

ISSUE_CLOUD_AUTH = "cloud_auth_failed"


def _local_client(address: str, creds: UnitCredentials) -> LocalUnitClient:
    return LocalUnitClient(address, creds)


def _pair(unit: UnitCredentials) -> tuple[str, str]:
    return unit.password_b64.reveal(), unit.crypto_serial_hex.reveal()


class KumoHub:
    """One per entry: object graph, lifecycle and device inventory."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options: dict[str, Any] = {**DEFAULT_OPTIONS, **entry.options}
        self.method = SetupMethod.parse(entry.data.get(CONF_SETUP_METHOD)) or SetupMethod.CLOUD_WS
        self.clock = SystemClock()
        self.ledger = CloudCallLedger(self.clock)
        username = entry.data.get(CONF_USERNAME)
        password = entry.data.get(CONF_PASSWORD)
        self.has_cloud = bool(username and password)
        self.local_capable = self.method is not SetupMethod.CLOUD_WS
        self.policy = RoutingPolicy.for_setup(self.method, self._mode(), has_cloud=self.has_cloud)
        self.devices: dict[str, KumoDevice] = {}
        self.links: dict[str, DeviceLink] = {}
        self.coordinators: dict[str, KumoDeviceCoordinator] = {}
        self.cn105_pollers: dict[str, Cn105Poller] = {}
        self.feeders: dict[str, RemoteTempFeeder] = {}
        self._stopping = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unit_services_started = False
        self._unsubs: list[Callable[[], None]] = []
        self._cidrs: list[str] = []
        self._known: dict[str, tuple[tuple[str, str], str]] = {}
        self._site_ids: list[str] = list(entry.data.get(CONF_SITE_IDS) or [])

        self.rest: CloudRestClient | None = None
        self.tokens: TokenManager | None = None
        self.socket: CloudSocketSession | None = None
        self.codec: CloudCodec | None = None
        self.cloud: CloudTransport | None = None
        self.leases: CloudLeaseManager | None = None
        if self.has_cloud:
            assert username is not None and password is not None
            client = get_async_client(hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)
            self.rest = CloudRestClient(client, self.ledger, self.clock)
            self.tokens = TokenManager(self.rest, username, password, self.clock)
            self.rest.bind_tokens(self.tokens)
            self.socket = CloudSocketSession(
                self.tokens,
                self.ledger,
                self.clock,
                idle_disconnect=float(self.options[CONF_SOCKET_IDLE_DISCONNECT]),
                refresh_on_connect=bool(self.options[CONF_REFRESH_ON_CONNECT]),
            )
            self.codec = CloudCodec()
            self.cloud = CloudTransport(self.rest, self.socket, self.codec, self.clock)
            self.leases = CloudLeaseManager(self.cloud, self.clock)

        self.credentials: CredentialService | None = None
        self.local: LocalTransport | None = None
        self.addresses: AddressResolver | None = None
        if self.local_capable:
            provider = None
            if self.rest is not None and self.socket is not None:
                provider = CloudCredentialProvider(
                    CloudInventory(self.rest, self._site_ids),
                    CryptoSerialSource(self.rest),
                    PasswordSource(self.socket),
                    self.clock,
                )
            self.credentials = CredentialService(
                HaCredentialRepository(hass, entry.unique_id or entry.entry_id),
                provider=provider,
                prober=SignedProber(),
                clock=self.clock,
            )
            codec = LocalCodec(
                local_room_temp_offset=bool(self.options[CONF_LOCAL_ROOM_TEMP_OFFSET])
            )
            self.local = LocalTransport(codec, client_factory=_local_client, clock=self.clock)
            self.addresses = AddressResolver(
                self.credentials,
                self.local,
                SubnetScanner(self.credentials),
                self.clock,
                cidrs_provider=lambda: list(self._cidrs),
            )

    def _mode(self) -> ConnectionMode:
        default = ConnectionMode(DEFAULT_MODE_BY_METHOD[self.method])
        mode = ConnectionMode.parse(self.options.get(CONF_CONNECTION_MODE)) or default
        if mode is ConnectionMode.CLOUD_ONLY and self.local_capable and not self.has_cloud:
            return ConnectionMode.LOCAL_ONLY
        return mode

    @property
    def poll_interval(self) -> timedelta:
        seconds = int(self.options[CONF_POLL_INTERVAL])
        return timedelta(seconds=min(max(seconds, MIN_POLL_INTERVAL), MAX_POLL_INTERVAL))

    @property
    def supports_room_temp_offset(self) -> bool:
        """Cloud relay, or local when the unverified local write is enabled."""
        return self.policy.has_cloud or (
            self.policy.has_local and bool(self.options[CONF_LOCAL_ROOM_TEMP_OFFSET])
        )

    @property
    def cn105_enabled(self) -> bool:
        """CN105 reads are on (local-capable entries only)."""
        return self.local is not None and bool(self.options[CONF_CN105_ENABLED])

    @property
    def cn105_codes(self) -> tuple[int, ...]:
        """Info codes to read; 0x06 only when explicitly selected."""
        chosen = {int(code) for code in self.options[CONF_CN105_CODES] or ()}
        codes = tuple(code for code in CN105_CODES if code in chosen)
        return codes or tuple(DEFAULT_CN105_CODES)

    @property
    def cn105_interval(self) -> float:
        return float(max(int(self.options[CONF_CN105_INTERVAL]), MIN_CN105_INTERVAL))

    async def async_start(self) -> None:
        """Start the hub as owned work."""
        await self.async_create_task(self._async_start(), "startup")

    async def _async_start(self) -> None:
        """Load credentials and inventory, then start links and first polls."""
        if self.method is SetupMethod.CLOUD_WS and not self.has_cloud:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="no_cloud_login")
        if self.credentials is not None and self.local is not None:
            await self.credentials.async_load()
            self._cidrs = list(self.options[CONF_CIDRS]) or await async_default_cidrs(self.hass)
            for serial, unit in self.credentials.all().items():
                if unit.has_secrets:
                    self.local.add_unit(serial, unit)
                    self._known[serial] = (_pair(unit), unit.address)
            await self._async_apply_ip_overrides()
            self._unsubs.append(self.credentials.add_listener(self._on_credentials_changed))
        if self.cloud is not None:
            self.cloud.set_listener(self._on_push)
        inventory = await self._async_load_inventory()
        for device, seed in inventory.values():
            await self._async_add_device(device, seed)
        await asyncio.gather(*(c.async_refresh() for c in self.coordinators.values()))
        self._request_missing_credentials()
        self._unit_services_started = True
        for serial in self.coordinators:
            self._start_unit_services(serial)
        if self.has_cloud:
            self._unsubs.append(
                async_track_time_interval(
                    self.hass,
                    self._on_inventory_tick,
                    timedelta(seconds=INVENTORY_REFRESH_S),
                    cancel_on_shutdown=True,
                )
            )

    async def async_stop(self) -> None:
        """Close coordinators, links, sockets and transports."""
        self._stopping = True
        while self._unsubs:
            self._unsubs.pop()()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        # CN105 reads stop first so feeder restore commands run before coordinators close.
        self._unit_services_started = False
        for poller in self.cn105_pollers.values():
            await poller.async_stop()
        for feeder in self.feeders.values():
            await feeder.async_stop()
        self.feeders.clear()
        self.cn105_pollers.clear()
        for coordinator in self.coordinators.values():
            await coordinator.async_shutdown()
        for link in self.links.values():
            await link.async_close()
        if self.leases is not None:
            await self.leases.async_close()
        if self.addresses is not None:
            await self.addresses.async_close()
        if self.cloud is not None:
            await self.cloud.async_close()
        if self.local is not None:
            await self.local.async_close()

    def _start_unit_services(self, serial: str) -> None:
        """CN105 reads and the remote temperature feeder; local-capable entries only."""
        if self._stopping:
            return
        coordinator = self.coordinators[serial]
        if self.local is None:
            return
        if self.cn105_enabled and serial not in self.cn105_pollers:
            poller = Cn105Poller(
                self.hass,
                self.entry,
                coordinator,
                self.local,
                codes=self.cn105_codes,
                interval=self.cn105_interval,
            )
            self.cn105_pollers[serial] = poller
            poller.async_start()
        mapping = self.remote_temp_mapping(serial)
        if (entity_id := mapping.get(CONF_RT_ENTITY)) and serial not in self.feeders:
            feeder = RemoteTempFeeder(
                self.hass,
                self.entry,
                coordinator,
                entity_id=entity_id,
                interval=float(mapping.get(CONF_RT_INTERVAL) or DEFAULT_RT_INTERVAL),
                manage_source=bool(mapping.get(CONF_RT_MANAGE_SOURCE, True)),
            )
            self.feeders[serial] = feeder
            feeder.async_start()

    def remote_temp_mapping(self, serial: str) -> Mapping[str, Any]:
        """Remote temperature options for serial; empty when unmapped."""
        mapping: Mapping[str, Any] = (self.options[CONF_REMOTE_TEMP] or {}).get(serial) or {}
        return mapping

    def async_create_task[T](self, coro: Coroutine[Any, Any, T], name: str) -> asyncio.Task[T]:
        """Create entry-owned work that is drained before transports close."""
        if self._stopping:
            coro.close()
            raise RuntimeError("hub is stopping")
        task = self.entry.async_create_background_task(
            self.hass, coro, f"{DOMAIN} {name}", eager_start=False
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        if self._stopping:
            coro.close()
            return
        self.async_create_task(coro, name)

    def _request_missing_credentials(self) -> None:
        """Auto mode: fetch credentials once for units that have none; they stay cloud-routed."""
        if self.credentials is None or self.local is None:
            return
        if self.policy.mode is not ConnectionMode.AUTO:
            return
        missing = sorted(s for s in self.devices if not self.local.has_unit(s))
        if missing:
            self._spawn(self._async_fetch_credentials(missing), "credential fetch")

    async def _async_fetch_credentials(self, serials: list[str]) -> None:
        assert self.credentials is not None
        result = await self.credentials.async_request_refresh(serials, reason="missing")
        _LOGGER.debug(
            "Credential fetch: refreshed=%s missing=%s error=%s",
            result.refreshed,
            sorted(result.missing),
            result.error,
        )

    async def _async_apply_ip_overrides(self) -> None:
        assert self.credentials is not None and self.local is not None
        overrides: Mapping[str, str] = self.options[CONF_IP_OVERRIDES] or {}
        for serial, address in overrides.items():
            if not address or self.credentials.get(serial) is None:
                continue
            await self.credentials.async_set_address(serial, address, pinned=True)
            if self.local.has_unit(serial):
                self.local.update_address(serial, address)
                self._known[serial] = (self._known[serial][0], address)

    def _stored_inventory(self) -> _Inventory:
        if self.credentials is None:
            return {}
        serials = {*self.credentials.all(), *self.credentials.meta_serials()}
        return {serial: (self._stored_device(serial), {}) for serial in sorted(serials)}

    def _stored_device(self, serial: str) -> KumoDevice:
        assert self.credentials is not None
        unit = self.credentials.get(serial)
        meta = self.credentials.meta(serial)
        return KumoDevice(
            serial=serial,
            name=meta.get("name") or (unit.label if unit else "") or serial,
            site_id=meta.get("site_id", ""),
            mac=(unit.mac if unit else "") or meta.get("mac", ""),
            model=meta.get("model"),
        )

    def _cloud_required(self, serials: Iterable[str]) -> bool:
        """Cloud is required unless every unit is currently routed locally."""
        if self.method is SetupMethod.CLOUD_WS or self.policy.mode is ConnectionMode.CLOUD_ONLY:
            return True
        local = self.local
        serials = tuple(serials)
        return (
            local is None
            or not serials
            or any(
                not local.has_unit(serial)
                or (link := self.links.get(serial)) is None
                or link.active_transport is not TransportKind.LOCAL
                for serial in serials
            )
        )

    async def _async_load_inventory(self) -> _Inventory:
        stored = self._stored_inventory()
        if not self.has_cloud:
            return stored
        try:
            inventory = await self._async_cloud_inventory()
        except AuthenticationError as err:
            if self._cloud_required(stored):
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="cloud_auth_failed"
                ) from err
            self._create_auth_issue()
            return stored
        except KumoError as err:
            if stored:
                _LOGGER.warning("Cloud inventory unavailable, using stored devices: %s", err)
                return stored
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cloud_unavailable",
                translation_placeholders={"error": type(err).__name__},
            ) from err
        self._clear_auth_issue()
        await self._async_save_inventory(inventory)
        return inventory

    async def _async_cloud_inventory(self) -> _Inventory:
        assert self.rest is not None and self.codec is not None
        result: _Inventory = {}
        records: list[ZoneRecord] = await CloudInventory(self.rest, self._site_ids).async_zones()
        for record in records:
            decoded = self.codec.decode_zone(record.zone)
            if decoded is None:
                continue
            serial, values = decoded
            meta = self.credentials.meta(serial) if self.credentials is not None else {}
            unit = self.credentials.get(serial) if self.credentials is not None else None
            has_mhk2 = record.adapter.get("hasMhk2")
            device = KumoDevice(
                serial=serial,
                name=values.get("name") or serial,
                site_id=record.site_id,
                mac=record.mac or (unit.mac if unit else "") or meta.get("mac", ""),
                model=values.get("model_number") or meta.get("model"),
                has_mhk2=has_mhk2 if isinstance(has_mhk2, bool) else None,
            )
            result[serial] = (device, values)
        for serial, (device, values) in result.items():
            if device.model is not None or not self.ledger.allow(False):
                continue
            try:
                details = await self.rest.get_device(serial)
            except KumoError as err:
                _LOGGER.debug("Device details for %s unavailable: %s", serial, err)
                continue
            extra = self.codec.decode_device(details)
            device.model = extra.get("model_number")
            result[serial] = (device, {**extra, **values})
        return result

    async def _async_save_inventory(self, inventory: _Inventory) -> None:
        """Cache names, sites and models so local-capable entries start offline."""
        creds = self.credentials
        if creds is None:
            return
        for serial, (device, _values) in inventory.items():
            meta = {"name": device.name, "site_id": device.site_id, "mac": device.mac}
            if device.model:
                meta["model"] = device.model
            if any(creds.meta(serial).get(k) != v for k, v in meta.items()):
                await creds.async_set_meta(serial, **meta)

    async def _async_add_device(self, device: KumoDevice, seed: Mapping[str, Any]) -> None:
        if self._stopping:
            return
        serial = device.serial
        link = DeviceLink(
            serial,
            policy=self.policy,
            health=HealthTracker(self.clock),
            local=self.local,
            cloud=self.cloud,
            creds=self.credentials,
            addresses=self.addresses,
            leases=self.leases,
            clock=self.clock,
            validator=CommandValidator(),
        )
        link.need_profile = self.cloud is not None
        if self.cloud is not None and device.site_id:
            self.cloud.register(serial, device.site_id)
        if not device.mac and self.credentials is not None:
            unit = self.credentials.get(serial)
            device.mac = unit.mac if unit else ""
        self.devices[serial] = device
        self.links[serial] = link
        self.coordinators[serial] = KumoDeviceCoordinator(
            self.hass,
            self.entry,
            self,
            device,
            link,
            poll_interval=self.poll_interval,
            seed=seed,
        )
        await link.async_start()

    async def _async_add_new_device(self, device: KumoDevice, seed: Mapping[str, Any]) -> None:
        """Add a device after startup and tell the platforms."""
        if self._stopping:
            return
        await self._async_add_device(device, seed)
        await self.coordinators[device.serial].async_refresh()
        if self._unit_services_started:
            self._start_unit_services(device.serial)
        async_dispatcher_send(self.hass, new_device_signal(self.entry.entry_id), device.serial)

    @callback
    def _on_inventory_tick(self, _now: datetime) -> None:
        self._spawn(self.async_refresh_inventory(), "inventory")

    async def async_refresh_inventory(self) -> None:
        """Refresh inventory as hub-owned work."""
        if self._stopping:
            return
        await self.async_create_task(self._async_refresh_inventory(), "inventory refresh")

    async def _async_refresh_inventory(self) -> None:
        """Daily: update names and add new devices."""
        try:
            inventory = await self._async_cloud_inventory()
        except AuthenticationError:
            self._create_auth_issue()
            return
        except KumoError as err:
            _LOGGER.debug("Inventory refresh failed: %s", err)
            return
        self._clear_auth_issue()
        await self._async_save_inventory(inventory)
        registry = dr.async_get(self.hass)
        for serial, (device, seed) in inventory.items():
            known = self.devices.get(serial)
            if known is None:
                await self._async_add_new_device(device, seed)
                continue
            if device.name != known.name or (device.model and device.model != known.model):
                known.name = device.name
                known.model = device.model or known.model
                entry = registry.async_get_device(identifiers={(DOMAIN, serial)})
                if entry is not None:
                    registry.async_update_device(entry.id, name=known.name, model=known.model)

    @callback
    def async_check_cloud_auth(self) -> None:
        """Called on each poll: a rejected login needs reauth only if cloud is required."""
        if self._stopping or self.tokens is None or not self.tokens.auth_failed:
            return
        if self._cloud_required(self.devices):
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="cloud_auth_failed"
            )
        self._create_auth_issue()

    def _create_auth_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_CLOUD_AUTH}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_CLOUD_AUTH,
            translation_placeholders={"title": self.entry.title},
        )

    def _clear_auth_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_CLOUD_AUTH}_{self.entry.entry_id}")

    @callback
    def _on_push(self, serial: str, patch: StatePatch) -> None:
        if self._stopping:
            return
        link = self.links.get(serial)
        coordinator = self.coordinators.get(serial)
        if link is not None and coordinator is not None:
            coordinator.async_handle_push(link.on_push(patch))

    @callback
    def _on_credentials_changed(self, serials: set[str]) -> None:
        if self._stopping:
            return
        assert self.credentials is not None and self.local is not None
        for serial in serials:
            unit = self.credentials.get(serial)
            if unit is None or not unit.has_secrets:
                continue
            pair, address = _pair(unit), unit.address
            known = self._known.get(serial)
            if known is None or not self.local.has_unit(serial):
                self.local.add_unit(serial, unit)
                creds_changed, address_changed = True, False
            else:
                creds_changed, address_changed = known[0] != pair, known[1] != address
                if creds_changed:
                    self.local.update_credentials(serial, unit)
                if address_changed:
                    self.local.update_address(serial, address)
            self._known[serial] = (pair, address)
            link = self.links.get(serial)
            if link is None:
                if serial not in self.devices:
                    self._spawn(
                        self._async_add_new_device(self._stored_device(serial), {}),
                        f"{serial} add",
                    )
                continue
            if creds_changed:
                self._spawn(link.async_on_credentials_changed(), f"{serial} credentials")
            elif address_changed:
                self._spawn(link.async_on_address_changed(), f"{serial} address")

    def serial_for_mac(self, mac: str) -> str | None:
        """Serial of the unit or device with this MAC, if it belongs to this entry."""
        target = dr.format_mac(mac)
        units = self.credentials.all() if self.credentials is not None else {}
        for serial, unit in units.items():
            if unit.mac and dr.format_mac(unit.mac) == target:
                return serial
        for serial, device in self.devices.items():
            if device.mac and dr.format_mac(device.mac) == target:
                return serial
        return None

    async def async_note_dhcp(self, mac: str, ip: str) -> bool:
        """Handle a DHCP sighting; True when the adapter belongs to this entry."""
        if self._stopping:
            return False
        return await self.async_create_task(self._async_note_dhcp(mac, ip), "dhcp")

    async def _async_note_dhcp(self, mac: str, ip: str) -> bool:
        serial = self.serial_for_mac(mac)
        if serial is None:
            return False
        unit = self.credentials.get(serial) if self.credentials is not None else None
        if self.credentials is None or unit is None:
            return True
        await self.credentials.async_set_address(serial, ip)
        updated = self.credentials.get(serial)
        link = self.links.get(serial)
        if link is not None and updated is not None and updated.address == unit.address:
            self._spawn(link.async_on_address_changed(), f"{serial} dhcp")
        return True
