"""Config, options, reauth, reconfigure and DHCP flows."""

import asyncio
import hashlib
import ipaddress
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.util.ssl import SSL_ALPN_HTTP11_HTTP2

from .adapters import (
    CloudInventory,
    CryptoSerialSource,
    PasswordSource,
    SignedProber,
    async_discover_addresses,
)
from .const import (
    CN105_CODES,
    CONF_CIDRS,
    CONF_CN105_CODES,
    CONF_CN105_ENABLED,
    CONF_CN105_INTERVAL,
    CONF_CONNECTION_MODE,
    CONF_IP_OVERRIDES,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_ON_CONNECT,
    CONF_REMOTE_TEMP,
    CONF_RT_ENTITY,
    CONF_RT_INTERVAL,
    CONF_RT_MANAGE_SOURCE,
    CONF_SETUP_METHOD,
    CONF_SITE_IDS,
    CONF_SOCKET_IDLE_DISCONNECT,
    CONF_TARGET_TEMP_STEP,
    CREDENTIAL_FETCH_TIMEOUT_S,
    DEFAULT_CN105_CODES,
    DEFAULT_OPTIONS,
    DEFAULT_RT_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_CN105_INTERVAL,
    MIN_POLL_INTERVAL,
    TARGET_TEMP_STEPS,
)
from .legacy_import import LegacySetup, async_detect, mask_username
from .pykumo2.clock import SystemClock
from .pykumo2.cloud.budget import CloudCallLedger
from .pykumo2.cloud.rest import CloudRestClient
from .pykumo2.cloud.socket import CloudSocketSession
from .pykumo2.cloud.tokens import TokenManager
from .pykumo2.credentials import codec
from .pykumo2.credentials.models import UnitCredentials
from .pykumo2.credentials.provider import CloudCredentialProvider, FetchResult
from .pykumo2.credentials.service import CredentialService, RefreshResult
from .pykumo2.domain.enums import SetupMethod
from .pykumo2.errors import AuthenticationError, KumoError
from .storage import async_load_service, async_save_units

_LOGGER = logging.getLogger(__name__)

TITLE = "Mitsubishi Comfort"
TITLE_LOCAL = "Mitsubishi Comfort (local)"

CONF_BACKUP = "backup"
CONF_ACTION = "action"
CONF_UNIT = "unit"
CONF_ADDRESS = "address"
CONF_RESCAN = "rescan"
CONF_USE_ACCOUNT = "use_account"
CONF_DISABLE_SOURCE = "disable_source_entry"
CONF_SKIP_ACCOUNT = "skip_account"

MAX_SCAN_ADDRESSES = 1024
MAX_SUMMARY_ITEMS = 10
MODES = ["auto", "local_only", "cloud_only"]

LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
MULTILINE = TextSelector(TextSelectorConfig(multiline=True))
CIDR_LIST = TextSelector(TextSelectorConfig(multiple=True))


def local_unique_id(serials: Iterable[str]) -> str:
    """unique_id of a local_backup entry: local:{sha1(sorted serials)[:12]}."""
    joined = ",".join(sorted(serials))
    return "local:" + hashlib.sha1(joined.encode(), usedforsecurity=False).hexdigest()[:12]


def _valid_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _valid_cidr(text: str) -> bool:
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return False
    return network.num_addresses <= MAX_SCAN_ADDRESSES


def _summarize(items: Iterable[str]) -> str:
    """Comma list of the first items; error strings carry serials and reasons only."""
    items = list(items)
    if not items:
        return "-"
    text = ", ".join(items[:MAX_SUMMARY_ITEMS])
    if len(items) > MAX_SUMMARY_ITEMS:
        text += f" (+{len(items) - MAX_SUMMARY_ITEMS})"
    return text


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ") or "-"


def _unit_rows(units: Mapping[str, UnitCredentials], found: Mapping[str, str] | None) -> str:
    """Markdown rows: serial, label, address, verified. Never secrets."""
    rows = []
    for serial, unit in sorted(units.items()):
        address = (found or {}).get(serial) or unit.address
        if unit.address_pinned:
            address += " (pinned)"
        verified = serial in found if found is not None else unit.verified_at is not None
        rows.append(
            f"| {_cell(serial)} | {_cell(unit.label)} | {_cell(address)} | "
            f"{'yes' if verified else 'no'} |"
        )
    return "\n".join(rows) or "| - | - | - | - |"


def _usable(unit: UnitCredentials) -> bool:
    try:
        unit.validate()
    except ValueError:
        return False
    return True


def _cn105_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CN105_ENABLED, default=False): BooleanSelector(),
            vol.Optional(
                CONF_CN105_CODES, default=[str(code) for code in DEFAULT_CN105_CODES]
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[str(code) for code in CN105_CODES],
                    multiple=True,
                    translation_key="cn105_codes",
                )
            ),
        }
    )


def _cn105_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    codes = {int(code) for code in user_input.get(CONF_CN105_CODES) or []}
    return {
        CONF_CN105_ENABLED: bool(user_input[CONF_CN105_ENABLED]),
        CONF_CN105_CODES: [code for code in CN105_CODES if code in codes],
    }


def _cloud_client(
    flow: ConfigFlow, login: Mapping[str, str], clock: SystemClock, ledger: CloudCallLedger
) -> tuple[CloudRestClient, TokenManager]:
    client = get_async_client(flow.hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)
    rest = CloudRestClient(client, ledger, clock)
    tokens = TokenManager(rest, login[CONF_USERNAME], login[CONF_PASSWORD], clock)
    rest.bind_tokens(tokens)
    return rest, tokens


class KumoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Mitsubishi Comfort config flow."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._method = SetupMethod.CLOUD_WS
        self._login: dict[str, str] = {}
        self._pending_login: dict[str, str] = {}
        self._login_errors: dict[str, str] = {}
        self._sites: dict[str, str] = {}
        self._site_ids: list[str] = []
        self._units: dict[str, UnitCredentials] = {}
        self._decode_errors: list[str] = []
        self._cidrs: list[str] = []
        self._cn105: dict[str, Any] = {}
        self._addresses: dict[str, str] = {}
        self._missing: dict[str, str] = {}
        self._fetch_failed = False
        self._task: asyncio.Task[Any] | None = None
        self._legacy: LegacySetup | None = None
        self._disable_legacy = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "KumoOptionsFlow":
        """Options flow."""
        return KumoOptionsFlow()

    async def _async_fetch_sites(self, login: Mapping[str, str]) -> dict[str, str]:
        """Log in and list sites; raises AuthenticationError or KumoError."""
        clock = SystemClock()
        rest, _tokens = _cloud_client(self, login, clock, CloudCallLedger(clock))
        try:
            sites = await rest.get_sites()
        finally:
            await rest.async_close()
        return {str(s["id"]): str(s.get("name") or s["id"]) for s in sites if s.get("id")}

    async def _async_validate(self, login: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        try:
            self._sites = await self._async_fetch_sites(login)
        except AuthenticationError:
            errors["base"] = "invalid_auth"
        except KumoError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during login")
            errors["base"] = "unknown"
        return errors

    async def _async_fetch_credentials(self) -> FetchResult:
        """CloudCredentialProvider run over a temporary REST client and socket."""
        clock = SystemClock()
        ledger = CloudCallLedger(clock)
        rest, tokens = _cloud_client(self, self._login, clock, ledger)
        socket = CloudSocketSession(tokens, ledger, clock)
        provider = CloudCredentialProvider(
            CloudInventory(rest, self._site_ids),
            CryptoSerialSource(rest),
            PasswordSource(socket),
            clock,
        )
        try:
            async with asyncio.timeout(CREDENTIAL_FETCH_TIMEOUT_S + 30):
                return await provider.fetch(None, timeout=CREDENTIAL_FETCH_TIMEOUT_S)
        finally:
            await socket.async_close()
            await rest.async_close()

    def _create_entry(
        self, method: SetupMethod, mode: str, *, title: str = TITLE
    ) -> ConfigFlowResult:
        data: dict[str, Any] = {CONF_SETUP_METHOD: method.value}
        if self._login:
            data |= {**self._login, CONF_SITE_IDS: self._site_ids}
        options = {
            **DEFAULT_OPTIONS,
            **self._cn105,
            CONF_CONNECTION_MODE: mode,
            CONF_CIDRS: self._cidrs,
        }
        return self.async_create_entry(title=title, data=data, options=options)

    async def _async_disable_legacy(self) -> None:
        """Disable the hass-kumo entry if asked; its files are never touched."""
        entry = self._legacy.entry if self._legacy is not None else None
        if self._disable_legacy and entry is not None and entry.disabled_by is None:
            await self.hass.config_entries.async_set_disabled_by(
                entry.entry_id, ConfigEntryDisabler.USER
            )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a setup method."""
        menu = ["cloud_ws", "import_backup", "cloud_fetch"]
        self._legacy = await async_detect(self.hass)
        if self._legacy.found:
            menu.append("import_pykumo")
        return self.async_show_menu(step_id="user", menu_options=menu)

    async def async_step_import_backup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste a credentials backup or a hass-kumo kumo_cache."""
        return await self._async_backup_step("import_backup", user_input)

    async def _async_backup_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        skipped = "-"
        if user_input is not None:
            cidrs = [c.strip() for c in user_input.get(CONF_CIDRS) or [] if c.strip()]
            try:
                decoded = codec.decode(user_input[CONF_BACKUP])
            except ValueError:
                errors["base"] = "invalid_backup"
            else:
                skipped = _summarize(decoded.errors)
                if not decoded.units:
                    errors["base"] = "no_units"
                elif not all(_valid_cidr(c) for c in cidrs):
                    errors[CONF_CIDRS] = "invalid_cidr"
                else:
                    self._units = {unit.serial: unit for unit in decoded.units}
                    self._decode_errors = decoded.errors
                    self._cidrs = cidrs
                    uid = local_unique_id(self._units)
                    if self.source == SOURCE_RECONFIGURE:
                        if self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, uid):
                            return self.async_abort(reason="already_configured")
                    else:
                        self._method = SetupMethod.LOCAL_BACKUP
                        await self.async_set_unique_id(uid)
                        self._abort_if_unique_id_configured()
                    return await self.async_step_discover()
        schema = vol.Schema(
            {vol.Required(CONF_BACKUP): MULTILINE, vol.Optional(CONF_CIDRS): CIDR_LIST}
        )
        suggested = {CONF_CIDRS: user_input.get(CONF_CIDRS)} if user_input else None
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"skipped": skipped},
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Probe stored addresses, then scan the subnets for the rest."""
        if self._task is None:
            self._task = self.hass.async_create_task(
                async_discover_addresses(self.hass, self._units, self._cidrs),
                f"{DOMAIN} discovery",
                eager_start=False,
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="discover", progress_action="discover", progress_task=self._task
            )
        task, self._task = self._task, None
        try:
            self._addresses = task.result()
        except Exception:
            _LOGGER.exception("Unit discovery failed")
            self._addresses = {}
        return self.async_show_progress_done(next_step_id="confirm_addresses")

    async def async_step_confirm_addresses(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One address per unit, prefilled with what discovery verified."""
        errors: dict[str, str] = {}
        if user_input is not None:
            addresses = {s: str(user_input.get(s) or "").strip() for s in self._units}
            errors = {s: "invalid_ip" for s, a in addresses.items() if a and not _valid_ip(a)}
            if not errors:
                now = time.time()
                self._units = {
                    serial: replace(
                        unit,
                        address=addresses[serial],
                        verified_at=now
                        if addresses[serial] and self._addresses.get(serial) == addresses[serial]
                        else unit.verified_at,
                    )
                    for serial, unit in self._units.items()
                }
                return await self._async_finish_units()
        schema = vol.Schema(
            {
                vol.Optional(
                    serial,
                    description={"suggested_value": self._addresses.get(serial) or unit.address},
                ): str
                for serial, unit in sorted(self._units.items())
            }
        )
        return self.async_show_form(
            step_id="confirm_addresses",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "units": _unit_rows(self._units, self._addresses),
                "skipped": _summarize(self._decode_errors),
            },
        )

    async def _async_finish_units(self) -> ConfigFlowResult:
        """Reconfigure updates the entry; a new entry asks about CN105 first."""
        if self.source == SOURCE_RECONFIGURE:
            units = list(self._units.values())
            entry = self._get_reconfigure_entry()
            await async_save_units(self.hass, entry.unique_id or entry.entry_id, units)
            return self.async_update_reload_and_abort(
                entry,
                data_updates={CONF_SETUP_METHOD: SetupMethod.CLOUD_FETCH.value},
                options={
                    **DEFAULT_OPTIONS,
                    **entry.options,
                    CONF_CONNECTION_MODE: "auto",
                    CONF_CIDRS: self._cidrs,
                },
            )
        if not any(_usable(unit) for unit in self._units.values()):
            return await self._async_create_local_entry()
        return await self.async_step_cn105()

    async def async_step_cn105(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Opt in to CN105 telemetry; offered only with usable local credentials."""
        if user_input is None:
            return self.async_show_form(
                step_id="cn105", data_schema=_cn105_schema(), last_step=True
            )
        self._cn105 = _cn105_options(user_input)
        return await self._async_create_local_entry()

    async def _async_create_local_entry(self) -> ConfigFlowResult:
        """Write the Store first, then create the entry."""
        units = list(self._units.values())
        assert self.unique_id is not None
        await async_save_units(self.hass, self.unique_id, units)
        if self._method is SetupMethod.CLOUD_FETCH:
            return self._create_entry(SetupMethod.CLOUD_FETCH, "auto")
        await self._async_disable_legacy()
        mode = "auto" if self._login else "local_only"
        title = TITLE if self._login else TITLE_LOCAL
        return self._create_entry(SetupMethod.LOCAL_BACKUP, mode, title=title)

    async def async_step_cloud_ws(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cloud websocket only."""
        self._method = SetupMethod.CLOUD_WS
        return await self.async_step_cloud_login()

    async def async_step_cloud_fetch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fetch local credentials from the cloud (currently broken)."""
        self._method = SetupMethod.CLOUD_FETCH
        return await self.async_step_cloud_login()

    async def async_step_cloud_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Kumo Cloud login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()
            errors = await self._async_validate(user_input)
            if not errors:
                self._login = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                return await self.async_step_sites()
        suggested = {CONF_USERNAME: user_input[CONF_USERNAME]} if user_input else None
        return self.async_show_form(
            step_id="cloud_login",
            data_schema=self.add_suggested_values_to_schema(LOGIN_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_sites(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick sites."""
        if user_input is not None:
            self._site_ids = list(user_input.get(CONF_SITE_IDS) or self._sites)
            if self._method is SetupMethod.CLOUD_FETCH:
                return await self.async_step_fetch_credentials()
            if self._method is SetupMethod.LOCAL_BACKUP:
                return await self.async_step_discover()
            await self._async_disable_legacy()
            return self._create_entry(SetupMethod.CLOUD_WS, "cloud_only")
        schema = vol.Schema(
            {vol.Optional(CONF_SITE_IDS, default=list(self._sites)): cv.multi_select(self._sites)}
        )
        return self.async_show_form(
            step_id="sites",
            data_schema=schema,
            description_placeholders={"site_count": str(len(self._sites))},
        )

    async def async_step_fetch_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fetch unit credentials through the cloud (60 s budget)."""
        if self._task is None:
            self._task = self.hass.async_create_task(
                self._async_fetch_credentials(), f"{DOMAIN} credential fetch", eager_start=False
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="fetch_credentials",
                progress_action="fetch_credentials",
                progress_task=self._task,
            )
        task, self._task = self._task, None
        self._fetch_failed = False
        fetched = FetchResult()
        try:
            fetched = task.result()
        except (KumoError, TimeoutError) as err:
            _LOGGER.warning("Credential fetch failed: %s", type(err).__name__)
            self._fetch_failed = True
        except Exception:
            _LOGGER.exception("Unexpected error during credential fetch")
            self._fetch_failed = True
        self._units.update({unit.serial: unit for unit in fetched.units})
        self._missing = {s: r for s, r in fetched.missing.items() if s not in self._units}
        if self._fetch_failed or self._missing:
            return self.async_show_progress_done(next_step_id="fetch_result")
        return self.async_show_progress_done(next_step_id="discover")

    async def async_step_fetch_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Some units have no credentials: retry, continue (cloud-routed) or abort."""
        if user_input is not None:
            action = user_input[CONF_ACTION]
            if action == "retry":
                return await self.async_step_fetch_credentials()
            if action == "abort":
                return self.async_abort(reason="fetch_aborted")
            if self._units:
                return await self.async_step_discover()
            return self._create_entry(SetupMethod.CLOUD_FETCH, "auto")
        schema = vol.Schema(
            {
                vol.Required(CONF_ACTION, default="retry"): SelectSelector(
                    SelectSelectorConfig(
                        options=["retry", "continue", "abort"], translation_key="fetch_action"
                    )
                )
            }
        )
        missing = [f"{serial} ({reason})" for serial, reason in sorted(self._missing.items())]
        return self.async_show_form(
            step_id="fetch_result",
            data_schema=schema,
            errors={"base": "credential_fetch_failed"} if self._fetch_failed else {},
            description_placeholders={
                "fetched": str(len(self._units)),
                "missing": _summarize(missing),
            },
        )

    async def async_step_import_pykumo(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Summary of what was found, then import it."""
        if user_input is None or self._legacy is None:
            self._legacy = await async_detect(self.hass)
        legacy = self._legacy
        if not (legacy.units or legacy.has_account):
            return self.async_abort(reason="no_pykumo_data")
        if user_input is not None:
            self._disable_legacy = bool(user_input.get(CONF_DISABLE_SOURCE))
            self._units = {unit.serial: unit for unit in legacy.units}
            self._decode_errors = list(legacy.errors)
            if user_input.get(CONF_USE_ACCOUNT) and legacy.has_account:
                assert legacy.username is not None and legacy.password is not None
                self._method = SetupMethod.LOCAL_BACKUP if self._units else SetupMethod.CLOUD_WS
                login = {CONF_USERNAME: legacy.username, CONF_PASSWORD: legacy.password}
                return await self._async_pykumo_login(login)
            return await self._async_pykumo_local()
        schema: dict[vol.Marker, Any] = {}
        if legacy.has_account:
            schema[vol.Required(CONF_USE_ACCOUNT, default=True)] = BooleanSelector()
        if legacy.entry is not None:
            schema[vol.Required(CONF_DISABLE_SOURCE, default=False)] = BooleanSelector()
        entry = "-"
        if legacy.entry is not None:
            entry = legacy.entry.title
            if legacy.entry_count > 1:
                entry += f" (1 of {legacy.entry_count})"
        return self.async_show_form(
            step_id="import_pykumo",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "files": _summarize(legacy.files),
                "entry": entry,
                "unit_count": str(len(legacy.units)),
                "address_count": str(sum(1 for unit in legacy.units if unit.address)),
                "account": mask_username(legacy.username)
                if legacy.has_account and legacy.username
                else "-",
            },
        )

    async def _async_pykumo_local(self) -> ConfigFlowResult:
        """Import units without a cloud login."""
        if not self._units:
            return self.async_abort(reason="no_pykumo_data")
        self._method = SetupMethod.LOCAL_BACKUP
        self._login = {}
        await self.async_set_unique_id(local_unique_id(self._units))
        self._abort_if_unique_id_configured()
        return await self.async_step_discover()

    async def _async_pykumo_login(self, login: dict[str, str]) -> ConfigFlowResult:
        await self.async_set_unique_id(login[CONF_USERNAME])
        self._abort_if_unique_id_configured()
        self._login_errors = await self._async_validate(login)
        if self._login_errors:
            self._pending_login = login
            return await self.async_step_pykumo_login()
        self._login = login
        return await self.async_step_sites()

    async def async_step_pykumo_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stored login was rejected: new password, or continue without the account."""
        errors = self._login_errors
        if user_input is not None:
            if user_input.get(CONF_SKIP_ACCOUNT):
                return await self._async_pykumo_local()
            login = {
                CONF_USERNAME: self._pending_login[CONF_USERNAME],
                CONF_PASSWORD: user_input.get(CONF_PASSWORD) or self._pending_login[CONF_PASSWORD],
            }
            return await self._async_pykumo_login(login)
        schema = vol.Schema(
            {
                vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
                vol.Required(CONF_SKIP_ACCOUNT, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="pykumo_login",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "username": mask_username(self._pending_login[CONF_USERNAME])
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Cloud login was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            login = {CONF_USERNAME: entry.data[CONF_USERNAME], **user_input}
            errors = await self._async_validate(login)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """cloud_ws: import a backup; local methods: add or replace the cloud login."""
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_SETUP_METHOD) == SetupMethod.CLOUD_WS.value:
            return await self.async_step_reconfigure_import()
        return await self.async_step_reconfigure_login()

    async def async_step_reconfigure_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import a backup into a cloud_ws entry; it becomes local first (auto)."""
        return await self._async_backup_step("reconfigure_import", user_input)

    async def async_step_reconfigure_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add or replace the cloud login; unlocks auto and cloud_only modes."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if entry.data.get(CONF_SETUP_METHOD) == SetupMethod.CLOUD_FETCH.value:
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
            errors = await self._async_validate(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_SITE_IDS: list(entry.data.get(CONF_SITE_IDS) or []),
                    },
                )
        username = (user_input or entry.data).get(CONF_USERNAME)
        return self.async_show_form(
            step_id="reconfigure_login",
            data_schema=self.add_suggested_values_to_schema(
                LOGIN_SCHEMA, {CONF_USERNAME: username} if username else None
            ),
            errors=errors,
        )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """A registered adapter renewed its lease: store the address for its unit."""
        mac, ip = discovery_info.macaddress, discovery_info.ip
        for entry in self._async_current_entries(include_ignore=False):
            if entry.state is ConfigEntryState.LOADED:
                if await entry.runtime_data.async_note_dhcp(mac, ip):
                    return self.async_abort(reason="already_configured")
                continue
            if entry.data.get(CONF_SETUP_METHOD) == SetupMethod.CLOUD_WS.value:
                continue
            try:
                service = await async_load_service(self.hass, entry.unique_id or entry.entry_id)
            except ValueError:
                continue
            target = dr.format_mac(mac)
            for serial, unit in service.all().items():
                if unit.mac and dr.format_mac(unit.mac) == target:
                    await service.async_set_address(serial, ip)
                    return self.async_abort(reason="already_configured")
        return self.async_abort(reason="not_kumo_device")


class KumoOptionsFlow(OptionsFlowWithReload):
    """Options menu, filtered by setup method. Credential steps never change options."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._serial = ""
        self._rescan = False
        self._export = "backup"
        self._task: asyncio.Task[Any] | None = None
        self._report: dict[str, str] = {}
        self._service: CredentialService | None = None

    @property
    def _method(self) -> SetupMethod:
        method = SetupMethod.parse(self.config_entry.data.get(CONF_SETUP_METHOD))
        return method or SetupMethod.CLOUD_WS

    @property
    def _has_cloud(self) -> bool:
        data = self.config_entry.data
        return bool(data.get(CONF_USERNAME) and data.get(CONF_PASSWORD))

    @property
    def _local_capable(self) -> bool:
        return self._method is not SetupMethod.CLOUD_WS

    def _current(self) -> dict[str, Any]:
        return {**DEFAULT_OPTIONS, **self.config_entry.options}

    def _hub(self) -> Any:
        entry = self.config_entry
        return entry.runtime_data if entry.state is ConfigEntryState.LOADED else None

    async def _async_credentials(self) -> CredentialService:
        """The loaded hub's service (hot reload), else one over the Store."""
        hub = self._hub()
        if hub is not None and hub.credentials is not None:
            service: CredentialService = hub.credentials
            return service
        if self._service is None:
            entry = self.config_entry
            self._service = await async_load_service(
                self.hass, entry.unique_id or entry.entry_id, SignedProber()
            )
        return self._service

    async def _async_unit_labels(self) -> dict[str, str]:
        """Serial to "Name (serial)", sorted by serial."""
        service = await self._async_credentials()
        labels = {}
        for serial, unit in sorted(service.all().items()):
            name = service.meta(serial).get("name") or unit.label or serial
            labels[serial] = serial if name == serial else f"{name} ({serial})"
        return labels

    async def _async_unit_options(self) -> list[SelectOptionDict]:
        labels = await self._async_unit_labels()
        return [SelectOptionDict(value=serial, label=label) for serial, label in labels.items()]

    def _finish_unchanged(self) -> ConfigFlowResult:
        """End without touching options, so the entry is not reloaded."""
        return self.async_create_entry(data=dict(self.config_entry.options))

    def _modes(self) -> list[str]:
        if self._method is SetupMethod.CLOUD_WS:
            return []
        if self._method is SetupMethod.LOCAL_BACKUP and not self._has_cloud:
            return ["local_only"]
        return MODES

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Options menu."""
        menu = ["connection", "climate"]
        if self._local_capable:
            menu += ["network", "cn105", "remote_temp", "credentials"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Mode, polling and socket settings."""
        current = self._current()
        if user_input is not None:
            return self.async_create_entry(data={**current, **user_input})
        schema: dict[vol.Marker, Any] = {}
        modes = self._modes()
        if len(modes) > 1:
            schema[vol.Required(CONF_CONNECTION_MODE)] = SelectSelector(
                SelectSelectorConfig(options=modes, translation_key="connection_mode")
            )
        schema[vol.Required(CONF_POLL_INTERVAL)] = vol.All(
            _seconds(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL), vol.Coerce(int)
        )
        if self._has_cloud:
            schema[vol.Required(CONF_SOCKET_IDLE_DISCONNECT)] = vol.All(
                _seconds(60, 3600), vol.Coerce(int)
            )
            schema[vol.Required(CONF_REFRESH_ON_CONNECT)] = BooleanSelector()
        return self.async_show_form(
            step_id="connection",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), current),
        )

    async def async_step_network(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan CIDRs, pinned addresses and a manual rescan."""
        current = self._current()
        errors: dict[str, str] = {}
        if user_input is not None:
            cidrs = [c.strip() for c in user_input.get(CONF_CIDRS) or [] if c.strip()]
            if not all(_valid_cidr(c) for c in cidrs):
                errors[CONF_CIDRS] = "invalid_cidr"
            else:
                self._options = {**current, CONF_CIDRS: cidrs}
                self._rescan = bool(user_input.get(CONF_RESCAN))
                if serial := user_input.get(CONF_UNIT):
                    self._serial = serial
                    return await self.async_step_network_pin()
                return await self._async_network_done()
        schema: dict[vol.Marker, Any] = {vol.Optional(CONF_CIDRS): CIDR_LIST}
        if units := await self._async_unit_options():
            schema[vol.Optional(CONF_UNIT)] = SelectSelector(SelectSelectorConfig(options=units))
        schema[vol.Required(CONF_RESCAN, default=False)] = BooleanSelector()
        pins = [f"{s} = {ip}" for s, ip in sorted(current[CONF_IP_OVERRIDES].items())]
        return self.async_show_form(
            step_id="network",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), {CONF_CIDRS: current[CONF_CIDRS]}
            ),
            errors=errors,
            description_placeholders={"pins": _summarize(pins)},
        )

    async def async_step_network_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pin an address for one unit, or clear the pin."""
        errors: dict[str, str] = {}
        overrides = dict(self._options[CONF_IP_OVERRIDES])
        if user_input is not None:
            address = str(user_input.get(CONF_ADDRESS) or "").strip()
            if address and not _valid_ip(address):
                errors[CONF_ADDRESS] = "invalid_ip"
            else:
                if address:
                    overrides[self._serial] = address
                else:
                    overrides.pop(self._serial, None)
                    await (await self._async_credentials()).async_clear_pin(self._serial)
                self._options[CONF_IP_OVERRIDES] = overrides
                return await self._async_network_done()
        service = await self._async_credentials()
        unit = service.get(self._serial)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ADDRESS, description={"suggested_value": overrides.get(self._serial)}
                ): str
            }
        )
        return self.async_show_form(
            step_id="network_pin",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "unit": (unit.label if unit else "") or self._serial,
                "address": (unit.address if unit else "") or "-",
            },
        )

    async def _async_network_done(self) -> ConfigFlowResult:
        if self._rescan:
            return await self.async_step_rescan()
        return self.async_create_entry(data=self._options)

    async def _async_rescan(self, cidrs: list[str]) -> dict[str, str]:
        service = await self._async_credentials()
        found = await async_discover_addresses(self.hass, service.all(), cidrs)
        for serial, address in found.items():
            await service.async_set_address(serial, address)
        return found

    async def async_step_rescan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Find units on the configured (or default) subnets now."""
        if self._task is None:
            self._task = self.hass.async_create_task(
                self._async_rescan(list(self._options[CONF_CIDRS])),
                f"{DOMAIN} rescan",
                eager_start=False,
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="rescan", progress_action="rescan", progress_task=self._task
            )
        task, self._task = self._task, None
        try:
            found: dict[str, str] = task.result()
        except Exception:
            _LOGGER.exception("Rescan failed")
            found = {}
        total = len((await self._async_credentials()).all())
        self._report = {"found": str(len(found)), "total": str(total)}
        return self.async_show_progress_done(next_step_id="rescan_result")

    async def async_step_rescan_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rescan summary."""
        if user_input is not None:
            return self.async_create_entry(data=self._options)
        return self.async_show_form(
            step_id="rescan_result",
            data_schema=vol.Schema({}),
            description_placeholders=self._report,
        )

    async def async_step_climate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Climate display options."""
        current = self._current()
        if user_input is not None:
            return self.async_create_entry(data={**current, **user_input})
        schema = vol.Schema(
            {
                vol.Required(CONF_TARGET_TEMP_STEP): SelectSelector(
                    SelectSelectorConfig(
                        options=list(TARGET_TEMP_STEPS),
                        mode=SelectSelectorMode.LIST,
                        translation_key="target_temp_step",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="climate", data_schema=self.add_suggested_values_to_schema(schema, current)
        )

    async def async_step_cn105(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """CN105 telemetry (experimental)."""
        current = self._current()
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **current,
                    **_cn105_options(user_input),
                    CONF_CN105_INTERVAL: int(user_input[CONF_CN105_INTERVAL]),
                }
            )
        schema = _cn105_schema().extend(
            {vol.Required(CONF_CN105_INTERVAL): _seconds(MIN_CN105_INTERVAL, 3600)}
        )
        suggested = {**current, CONF_CN105_CODES: [str(c) for c in current[CONF_CN105_CODES]]}
        return self.async_show_form(
            step_id="cn105", data_schema=self.add_suggested_values_to_schema(schema, suggested)
        )

    async def async_step_remote_temp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a unit to map a Home Assistant temperature sensor to."""
        labels = await self._async_unit_labels()
        if not labels:
            return self.async_abort(reason="no_units")
        if user_input is not None:
            self._serial = user_input[CONF_UNIT]
            return await self.async_step_remote_temp_unit()
        mapped = [
            f"{labels.get(serial, serial)} = {value.get(CONF_RT_ENTITY)}"
            for serial, value in sorted(self._current()[CONF_REMOTE_TEMP].items())
        ]
        units = [SelectOptionDict(value=serial, label=label) for serial, label in labels.items()]
        return self.async_show_form(
            step_id="remote_temp",
            data_schema=vol.Schema(
                {vol.Required(CONF_UNIT): SelectSelector(SelectSelectorConfig(options=units))}
            ),
            description_placeholders={"mapped": _summarize(mapped)},
        )

    async def async_step_remote_temp_unit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sensor, interval and source handling for one unit; no sensor removes the mapping."""
        current = self._current()
        mapping = {k: dict(v) for k, v in current[CONF_REMOTE_TEMP].items()}
        if user_input is not None:
            if entity_id := user_input.get(CONF_RT_ENTITY):
                mapping[self._serial] = {
                    CONF_RT_ENTITY: entity_id,
                    CONF_RT_INTERVAL: int(user_input[CONF_RT_INTERVAL]),
                    CONF_RT_MANAGE_SOURCE: bool(user_input[CONF_RT_MANAGE_SOURCE]),
                }
            else:
                mapping.pop(self._serial, None)
            return self.async_create_entry(data={**current, CONF_REMOTE_TEMP: mapping})
        existing = mapping.get(self._serial, {})
        labels = await self._async_unit_labels()
        schema = vol.Schema(
            {
                vol.Optional(CONF_RT_ENTITY): EntitySelector(
                    EntitySelectorConfig(
                        domain="sensor", device_class=SensorDeviceClass.TEMPERATURE
                    )
                ),
                vol.Required(CONF_RT_INTERVAL): _seconds(5, 300),
                vol.Required(CONF_RT_MANAGE_SOURCE): BooleanSelector(),
            }
        )
        suggested = {
            CONF_RT_ENTITY: existing.get(CONF_RT_ENTITY),
            CONF_RT_INTERVAL: existing.get(CONF_RT_INTERVAL, DEFAULT_RT_INTERVAL),
            CONF_RT_MANAGE_SOURCE: existing.get(CONF_RT_MANAGE_SOURCE, True),
        }
        return self.async_show_form(
            step_id="remote_temp_unit",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            description_placeholders={"unit": labels.get(self._serial, self._serial)},
        )

    # No update listener or reload here; the hub hot-reloads credentials itself.

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Credentials submenu."""
        menu = ["view_credentials", "export_kumo_cache", "import_credentials"]
        if self._has_cloud:
            menu.append("refresh_from_cloud")
        return self.async_show_menu(step_id="credentials", menu_options=menu)

    async def async_step_view_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warning before secrets are shown (backup format)."""
        self._export = "backup"
        if user_input is not None:
            return await self.async_step_credentials_copy()
        return self.async_show_form(step_id="view_credentials", data_schema=vol.Schema({}))

    async def async_step_export_kumo_cache(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warning before secrets are shown (kumo_cache format)."""
        self._export = "kumo_cache"
        if user_input is not None:
            return await self.async_step_credentials_copy()
        return self.async_show_form(step_id="export_kumo_cache", data_schema=vol.Schema({}))

    async def async_step_credentials_copy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Table without secrets; the JSON copy field is the only place they appear."""
        if user_input is not None:
            return self._finish_unchanged()
        service = await self._async_credentials()
        if self._export == "backup":
            text, fmt = service.export_backup(), "ha_kumo_ws backup"
        else:
            text, fmt = service.export_kumo_cache(), "kumo_cache"
        schema = vol.Schema(
            {vol.Optional(CONF_BACKUP, description={"suggested_value": text}): MULTILINE}
        )
        return self.async_show_form(
            step_id="credentials_copy",
            data_schema=schema,
            description_placeholders={"units": _unit_rows(service.all(), None), "format": fmt},
        )

    async def async_step_import_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Merge a backup, kumo_cache or kumo.cfg into the stored credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            service = await self._async_credentials()
            report = await service.async_import(user_input[CONF_BACKUP])
            rejected = {k: v for k, v in report.rejected.items() if k != "import"}
            if report.added or report.updated or report.unchanged or rejected:
                self._report = {
                    "added": _summarize(report.added),
                    "updated": _summarize(report.updated),
                    "unchanged": _summarize(report.unchanged),
                    "kept": _summarize(report.kept_old_pair),
                    "rejected": _summarize(f"{k}: {v}" for k, v in sorted(rejected.items())),
                }
                return await self.async_step_import_result()
            errors["base"] = "invalid_backup" if "import" in report.rejected else "no_units"
        return self.async_show_form(
            step_id="import_credentials",
            data_schema=vol.Schema({vol.Required(CONF_BACKUP): MULTILINE}),
            errors=errors,
        )

    async def async_step_import_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Merge report."""
        if user_input is not None:
            return self._finish_unchanged()
        return self.async_show_form(
            step_id="import_result",
            data_schema=vol.Schema({}),
            description_placeholders=self._report,
        )

    async def async_step_refresh_from_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fetch credentials from the cloud now (currently broken)."""
        hub = self._hub()
        if self._task is None:
            if hub is None or hub.credentials is None or hub._stopping:
                return self.async_abort(reason="not_loaded")
            serials = sorted({*hub.devices, *hub.credentials.all()})
            self._task = hub.async_create_task(
                hub.credentials.async_request_refresh(serials, reason="user", force=True),
                "credential refresh",
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="refresh_from_cloud",
                progress_action="refresh_from_cloud",
                progress_task=self._task,
            )
        task, self._task = self._task, None
        try:
            result: RefreshResult = task.result()
        except asyncio.CancelledError:
            result = RefreshResult(error="cancelled")
        except Exception as err:
            _LOGGER.error("Credential refresh failed: %s", type(err).__name__)
            result = RefreshResult(error="unknown")
        missing = [f"{serial} ({reason})" for serial, reason in sorted(result.missing.items())]
        self._report = {
            "refreshed": _summarize(result.refreshed),
            "missing": _summarize(missing),
            "error": result.error or "-",
        }
        return self.async_show_progress_done(next_step_id="refresh_result")

    async def async_step_refresh_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Refresh summary."""
        if user_input is not None:
            return self._finish_unchanged()
        return self.async_show_form(
            step_id="refresh_result",
            data_schema=vol.Schema({}),
            description_placeholders=self._report,
        )


def _seconds(minimum: int, maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            unit_of_measurement="s",
            mode=NumberSelectorMode.BOX,
        )
    )
