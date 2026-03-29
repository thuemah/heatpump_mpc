"""Heat Pump MPC — Home Assistant integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, SERVICE_GET_SH_HOURLY
from .coordinator import HeatpumpMpcCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "binary_sensor", "number"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Heat Pump MPC from a config entry."""
    coordinator = HeatpumpMpcCoordinator(hass, entry)

    # Load persisted learner and SH state before the first solve.
    await coordinator.async_setup()

    # Perform the first refresh so sensors have data immediately on startup.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the get_sh_hourly service once (shared across all instances).
    if not hass.services.has_service(DOMAIN, SERVICE_GET_SH_HOURLY):

        async def handle_get_sh_hourly(call: ServiceCall) -> dict:
            """Return the rolling SH hourly buffer for the requested instance.

            The optional ``entry_id`` parameter routes the call to a specific
            MPC instance when multiple are installed.  When omitted and exactly
            one instance is running, that instance is used automatically.
            """
            entry_id: str | None = call.data.get("entry_id")
            instances: dict = hass.data.get(DOMAIN, {})

            if entry_id:
                coord = instances.get(entry_id)
                if coord is None:
                    raise HomeAssistantError(
                        f"No Heat Pump MPC instance with entry_id {entry_id!r}"
                    )
                return {"buffer": list(coord.sh_hourly_buffer)}

            coordinators = list(instances.values())
            if not coordinators:
                return {"buffer": []}
            if len(coordinators) == 1:
                return {"buffer": list(coordinators[0].sh_hourly_buffer)}

            raise HomeAssistantError(
                "Multiple Heat Pump MPC instances are installed. "
                "Provide 'entry_id' to select the correct instance."
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_SH_HOURLY,
            handle_get_sh_hourly,
            schema=vol.Schema({vol.Optional("entry_id"): str}),
            supports_response=SupportsResponse.ONLY,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        # Remove the shared service when the last instance is unloaded.
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_GET_SH_HOURLY)
    return unload_ok
