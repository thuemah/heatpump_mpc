"""
Binary sensor platform for Heat Pump MPC.

Entities per instance
---------------------
binary_sensor.<name>_pump_on          Should the heat pump run this hour?
binary_sensor.<name>_schedule_feasible Did the solver satisfy all constraints?

Multi-instance
--------------
Unique IDs are scoped to ``entry.entry_id``.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    RESULT_PUMP_ON_NOW,
    RESULT_FEASIBLE,
    RESULT_OPTIMAL_LWT,
    RESULT_CURRENT_COP,
    RESULT_DHW_ON_NOW,
    RESULT_DHW_SETPOINT,
)
from .coordinator import HeatpumpMpcCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MPC binary sensor entities from a config entry."""
    coordinator: HeatpumpMpcCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            PumpOnSensor(coordinator, entry),
            ScheduleFeasibleSensor(coordinator, entry),
            DhwOnSensor(coordinator, entry),
        ]
    )


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class MpcBaseBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Shared base for all MPC binary sensor entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HeatpumpMpcCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Group all MPC entities under a single device in the HA UI."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Heat Pump MPC",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _raw(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)


# ---------------------------------------------------------------------------
# Concrete binary sensors
# ---------------------------------------------------------------------------


class PumpOnSensor(MpcBaseBinarySensor):
    """
    True when the MPC solver has scheduled the heat pump to run in the
    current hour.

    An automation watches this sensor and writes the optimal LWT setpoint
    to the heat pump's Modbus register whenever it turns on.
    """

    def __init__(
        self, coordinator: HeatpumpMpcCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, RESULT_PUMP_ON_NOW, "Pump On")
        self._attr_icon = "mdi:heat-pump"

    @property
    def is_on(self) -> bool | None:
        v = self._raw
        return bool(v) if v is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        """Surface the optimal LWT and COP alongside the on/off state."""
        data = self.coordinator.data or {}
        return {
            "optimal_lwt": data.get(RESULT_OPTIMAL_LWT),
            "estimated_cop": data.get(RESULT_CURRENT_COP),
        }


class DhwOnSensor(MpcBaseBinarySensor):
    """
    True when the MPC solver has scheduled DHW mode for the current hour.

    When True, write ``number.heat_pump_mpc_dhw_setpoint`` (= dhw_target_temp)
    to the heat pump's DHW target to trigger reheating.
    When False, writing the setpoint value (= dhw_min_temp − 1) to the HP
    blocks unsolicited DHW starts during expensive hours.

    The entity reports ``False`` (not unknown) when DHW scheduling is
    disabled so automations do not need a special null-handling branch.
    """

    def __init__(
        self, coordinator: HeatpumpMpcCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, RESULT_DHW_ON_NOW, "DHW Mode On")
        self._attr_icon = "mdi:water-boiler"

    @property
    def is_on(self) -> bool:
        v = self._raw
        return bool(v) if v is not None else False

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "recommended_dhw_setpoint": data.get(RESULT_DHW_SETPOINT),
        }


class ScheduleFeasibleSensor(MpcBaseBinarySensor):
    """
    True when the solver found a schedule that satisfies all tank constraints.

    False indicates the demand is higher than what the heat pump can deliver
    within the horizon — typically caused by a very high heat demand or a
    very cold spell where the tank cannot be pre-charged fast enough.  In
    this case the schedule still represents the best-effort plan.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HeatpumpMpcCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, RESULT_FEASIBLE, "Schedule Feasible")
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_icon = "mdi:check-circle-outline"

    @property
    def is_on(self) -> bool | None:
        """
        For the PROBLEM device class, ``True`` = problem detected.
        We invert feasible: infeasible → problem (True).
        """
        v = self._raw
        if v is None:
            return None
        return not bool(v)  # True = infeasible = problem
