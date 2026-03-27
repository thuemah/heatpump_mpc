"""
Number platform for Heat Pump MPC.

Exposes the MPC solver's recommended leaving water temperature (LWT) as a
writable ``NumberEntity``.  An HA automation can read this entity and write the
value to the heat pump's Modbus register or climate entity.

Behaviour
---------
* The entity auto-tracks ``coordinator.data[RESULT_OPTIMAL_LWT]`` on every
  coordinator refresh — no manual sync required.
* Writing to the entity (via the UI or a service call) stores a temporary
  override.  The value is reset to the solver's fresh recommendation on the
  next coordinator update (every 30 minutes).
* Min/max bounds are sourced directly from the config entry so they match the
  solver's own constraints.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_LWT_STEP,
    CONF_MAX_LWT,
    CONF_MIN_LWT,
    CONF_DHW_MIN_TEMP,
    CONF_DHW_TARGET_TEMP,
    DEFAULT_LWT_STEP,
    DEFAULT_MAX_LWT,
    DEFAULT_MIN_LWT,
    DEFAULT_DHW_MIN_TEMP,
    DEFAULT_DHW_TARGET_TEMP,
    DOMAIN,
    RESULT_OPTIMAL_LWT,
    RESULT_DHW_SETPOINT,
)
from .coordinator import HeatpumpMpcCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MPC number entities from a config entry."""
    coordinator: HeatpumpMpcCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LwtSetpointNumber(coordinator, entry),
        DhwSetpointNumber(coordinator, entry),
    ])


class LwtSetpointNumber(CoordinatorEntity, NumberEntity):
    """
    Recommended LWT setpoint from the MPC solver.

    The value is automatically updated to the solver's recommendation on every
    coordinator refresh.  Writing to the entity stores a temporary override
    that is replaced by the next solver recommendation.

    Use this entity as the source for an automation that writes the LWT
    setpoint to the heat pump (via Modbus, climate entity, or similar).
    """

    _attr_has_entity_name = True
    _attr_name = "LWT Setpoint"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermometer-water"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: HeatpumpMpcCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_lwt_setpoint"

        cfg = entry.data
        self._attr_native_min_value = float(cfg.get(CONF_MIN_LWT, DEFAULT_MIN_LWT))
        self._attr_native_max_value = float(cfg.get(CONF_MAX_LWT, DEFAULT_MAX_LWT))
        self._attr_native_step = float(cfg.get(CONF_LWT_STEP, DEFAULT_LWT_STEP))

        # Internal state — kept in sync with the coordinator recommendation.
        self._lwt: float | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Group all MPC entities under a single device in the HA UI."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Heat Pump MPC",
            entry_type=DeviceEntryType.SERVICE,
        )

    # ------------------------------------------------------------------
    # CoordinatorEntity hook
    # ------------------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Pull the latest solver recommendation and push a state update."""
        data = self.coordinator.data
        if data is not None:
            lwt = data.get(RESULT_OPTIMAL_LWT)
            if lwt is not None:
                self._lwt = float(lwt)
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # NumberEntity interface
    # ------------------------------------------------------------------

    @property
    def native_value(self) -> float | None:
        return self._lwt

    async def async_set_native_value(self, value: float) -> None:
        """Accept a manual override; reset to solver recommendation on next update."""
        self._lwt = value
        self.async_write_ha_state()
        _LOGGER.debug(
            "LWT setpoint manually set to %.1f °C (overrides solver until next update)",
            value,
        )


class DhwSetpointNumber(CoordinatorEntity, NumberEntity):
    """
    Recommended DHW tank target temperature from the MPC solver.

    Write this value to the heat pump's DHW setpoint register each hour:
    - When ``binary_sensor.dhw_on`` is True  → value equals ``dhw_target_temp``
      (HP will reheat DHW tank).
    - When ``binary_sensor.dhw_on`` is False → value equals ``dhw_min_temp − 1``
      (HP sees tank as "warm enough" and will not start DHW mode).

    Zero (0.0) when DHW scheduling is disabled — automations should check
    the value before writing to avoid accidentally setting the HP to 0 °C.
    """

    _attr_has_entity_name = True
    _attr_name = "DHW Setpoint"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:water-boiler"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: HeatpumpMpcCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_dhw_setpoint"

        cfg = entry.data
        dhw_min = float(cfg.get(CONF_DHW_MIN_TEMP, DEFAULT_DHW_MIN_TEMP))
        dhw_target = float(cfg.get(CONF_DHW_TARGET_TEMP, DEFAULT_DHW_TARGET_TEMP))
        self._attr_native_min_value = max(0.0, dhw_min - 5.0)
        self._attr_native_max_value = dhw_target + 5.0
        self._attr_native_step = 0.5

        self._setpoint: float | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Heat Pump MPC",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        if data is not None:
            sp = data.get(RESULT_DHW_SETPOINT)
            if sp is not None:
                self._setpoint = float(sp)
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._setpoint

    async def async_set_native_value(self, value: float) -> None:
        """Accept a manual override; reset on next coordinator update."""
        self._setpoint = value
        self.async_write_ha_state()
        _LOGGER.debug(
            "DHW setpoint manually set to %.1f °C (overrides solver until next update)",
            value,
        )
