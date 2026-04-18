"""Config flow for Converging Systems e-Node CS-Bus integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_SCAN_INTERVAL,
    CONF_DEFAULT_TRANSITION,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TRANSITION,
    DEFAULT_USERNAME,
    DOMAIN,
)
from .enode_client import ENodeClient

_LOGGER = logging.getLogger(__name__)


class CSBusENodeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the setup wizard for the e-Node integration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            username = user_input.get(CONF_USERNAME, DEFAULT_USERNAME)
            password = user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD)

            # Prevent duplicate entries for the same host
            await self.async_set_unique_id(host.lower())
            self._abort_if_unique_id_configured()

            # Test connectivity
            client = ENodeClient(host=host, port=port, username=username, password=password)
            try:
                ok = await asyncio.wait_for(client.async_connect(), timeout=12.0)
                if not ok:
                    errors["base"] = "cannot_connect"
                else:
                    await client.async_disconnect()
                    return self.async_create_entry(
                        title=user_input.get(CONF_NAME) or f"e-Node ({host})",
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                        },
                    )
            except asyncio.TimeoutError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during e-Node connection test")
                errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_NAME, default=""): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    int, vol.Range(min=1, max=65535)
                ),
                vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "default_port": str(DEFAULT_PORT),
                "default_user": DEFAULT_USERNAME,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> CSBusENodeOptionsFlow:
        return CSBusENodeOptionsFlow(config_entry)


class CSBusENodeOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to tweak scan interval and default transition."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                ): vol.All(int, vol.Range(min=10, max=300)),
                vol.Optional(
                    CONF_DEFAULT_TRANSITION,
                    default=self.config_entry.options.get(
                        CONF_DEFAULT_TRANSITION, DEFAULT_TRANSITION
                    ),
                ): vol.All(int, vol.Range(min=0, max=60)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
