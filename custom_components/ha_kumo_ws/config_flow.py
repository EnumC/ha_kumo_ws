"""Config flow for the Mitsubishi Comfort integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import CONF_REFRESH_ON_CONNECT, CONF_SITE_IDS, DOMAIN
from .pykumo2 import AuthenticationError, MitsubishiComfortClient


class MitsubishiComfortConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mitsubishi Comfort (Kumo Cloud)."""

    VERSION = 1
    _auth_data: dict | None = None
    _sites: list[dict] = []

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle credential collection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            refresh_on_connect = user_input.get(CONF_REFRESH_ON_CONNECT, True)

            client = MitsubishiComfortClient(username=username, password=password, site_ids=[])
            try:
                await client.async_login()
                self._sites = await client.async_get_sites()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            finally:
                await client.close()

            if not errors:
                self._auth_data = {
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                    CONF_REFRESH_ON_CONNECT: refresh_on_connect,
                }
                return await self.async_step_sites()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_REFRESH_ON_CONNECT, default=True): bool,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_sites(self, user_input: dict | None = None) -> FlowResult:
        """Let the user pick one or more sites."""
        if not self._auth_data:
            return await self.async_step_user()

        site_options = {str(site["id"]): site.get("name", site["id"]) for site in self._sites}
        if user_input is not None:
            site_ids: list[str] = user_input.get(CONF_SITE_IDS, [])
            await self.async_set_unique_id(self._auth_data[CONF_USERNAME])
            self._abort_if_unique_id_configured()

            data = {
                **self._auth_data,
                CONF_SITE_IDS: site_ids or list(site_options),
            }
            title = "Mitsubishi Comfort"
            return self.async_create_entry(title=title, data=data)

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SITE_IDS,
                    default=list(site_options),
                ): cv.multi_select(site_options)
            }
        )

        return self.async_show_form(
            step_id="sites",
            data_schema=data_schema,
            errors={},
            description_placeholders={
                "site_count": str(len(site_options)),
            },
        )
