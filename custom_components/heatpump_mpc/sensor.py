"""
Sensor platform for Heat Pump MPC.

Exposes the MPC solver output as Home Assistant sensor entities.  All
sensors are read-only and sourced from coordinator.data — no logic lives
here.

Entities per instance
---------------------
sensor.<name>_optimal_lwt        Target leaving water temperature (°C)
sensor.<name>_optimal_output_kw  Optimal inverter output level (kW)
sensor.<name>_estimated_cop      Effective COP for the current hour
sensor.<name>_total_cost         Projected electricity cost over horizon
sensor.<name>_next_run_start     ISO timestamp of next scheduled pump start

Multi-instance
--------------
Unique IDs are scoped to ``entry.entry_id`` so multiple MPC instances
(e.g. one per heat pump) can coexist without entity ID collisions.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    RESULT_OPTIMAL_LWT,
    RESULT_OPTIMAL_OUTPUT_KW,
    RESULT_CURRENT_COP,
    RESULT_TOTAL_COST,
    RESULT_NEXT_RUN_START,
    RESULT_SCHEDULE,
    RESULT_PLANNED_RUN_HOURS,
    RESULT_PLANNED_STARTS,
    RESULT_PLANNED_KWH_THERMAL,
    RESULT_PLANNED_KWH_ELECTRICAL,
)
from .coordinator import HeatpumpMpcCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MPC sensor entities from a config entry."""
    coordinator: HeatpumpMpcCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            OptimalLwtSensor(coordinator, entry),
            OptimalOutputSensor(coordinator, entry),
            EstimatedCopSensor(coordinator, entry),
            TotalCostSensor(coordinator, entry),
            NextRunStartSensor(coordinator, entry),
        ]
    )


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class MpcBaseSensor(CoordinatorEntity, SensorEntity):
    """Shared base for all MPC sensor entities."""

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
    def _value(self):
        """Return the raw value from coordinator.data, or None if absent."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)


# ---------------------------------------------------------------------------
# Concrete sensors
# ---------------------------------------------------------------------------


class OptimalLwtSensor(MpcBaseSensor):
    """
    Target leaving water temperature selected by the MPC solver.

    This is the setpoint that should be written to the heat pump's Modbus
    register (via a separate automation) when the pump is scheduled to run.
    """

    def __init__(
        self, coordinator: HeatpumpMpcCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, RESULT_OPTIMAL_LWT, "Optimal Flow Temperature")
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        v = self._value
        return round(float(v), 1) if v is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        """Expose horizon aggregates and the full hour-by-hour schedule."""
        data = self.coordinator.data or {}
        schedule = data.get(RESULT_SCHEDULE, [])
        return {
            # Aggregate horizon metrics (thermal vs electrical explicitly labelled)
            "planned_run_hours": data.get(RESULT_PLANNED_RUN_HOURS, 0),
            "planned_starts": data.get(RESULT_PLANNED_STARTS, 0),
            "planned_kwh_thermal": data.get(RESULT_PLANNED_KWH_THERMAL, 0.0),
            "planned_kwh_electrical": data.get(RESULT_PLANNED_KWH_ELECTRICAL, 0.0),
            # Full per-hour plan for Lovelace visualisation
            "schedule": [
                {
                    "hour": p.hour_index,
                    "pump_on": p.pump_on,
                    "start_event": p.start_event,
                    "lwt": p.lwt,
                    "output_kw": round(p.output_kw, 2),
                    "max_capacity_kw": round(p.max_capacity_kw, 2),
                    "cop": round(p.cop_effective, 2),
                    "cost_per_kwh_heat": round(p.cost_per_kwh_heat, 4),
                    "heat_delivered_kwh": round(p.heat_delivered_kwh, 3),
                    "tank_energy_kwh": round(p.tank_energy_kwh, 3),
                    "tank_temp_c": round(p.tank_temp_c, 1),
                    "electricity_cost": round(p.electricity_cost, 4),
                }
                for p in schedule
            ],
        }


class OptimalOutputSensor(MpcBaseSensor):
    """
    Optimal inverter output level selected by the MPC solver (kW).

    This is the thermal output the heat pump should be configured to run at
    when the pump is scheduled on.  A value below the rated output indicates
    the solver chose part-load operation for a better COP.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HeatpumpMpcCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, RESULT_OPTIMAL_OUTPUT_KW, "Optimal Output")
        self._attr_native_unit_of_measurement = "kW"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:heat-pump"

    @property
    def native_value(self) -> float | None:
        v = self._value
        return round(float(v), 2) if v is not None else None


class EstimatedCopSensor(MpcBaseSensor):
    """Effective COP estimated for the current hour at the optimal LWT."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HeatpumpMpcCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, RESULT_CURRENT_COP, "Estimated COP")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:heat-pump"

    @property
    def native_value(self) -> float | None:
        v = self._value
        return round(float(v), 2) if v is not None else None


class TotalCostSensor(MpcBaseSensor):
    """
    Projected electricity cost for the full optimisation horizon.

    Unit is whatever currency the price sensor uses (e.g. NOK).
    Useful for comparing scenario costs, not for billing.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HeatpumpMpcCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, RESULT_TOTAL_COST, "Projected Heating Cost")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:cash-clock"

    @property
    def native_value(self) -> float | None:
        v = self._value
        return round(float(v), 2) if v is not None else None


class NextRunStartSensor(MpcBaseSensor):
    """
    Timestamp of the next scheduled heat pump start.

    ``None`` when no pump-on hour is found in the current schedule
    (e.g. tank is already warm enough for the full horizon).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HeatpumpMpcCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, RESULT_NEXT_RUN_START, "Next Run Start")
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-start"

    @property
    def native_value(self) -> datetime | None:
        v = self._value
        if v is None:
            return None
        from homeassistant.util import dt as dt_util
        return dt_util.parse_datetime(v)
