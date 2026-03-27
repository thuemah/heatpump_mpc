"""
Config flow for Heat Pump MPC.

Four-step setup
---------------
Step 1 — Data sources      : HA instance, price sensor, tank sensor, weather.
Step 2 — Heat pump & tank  : Physical parameters (LWT range, output, volume).
Step 3 — COP learning      : Measurement sensors for runtime η-calibration.
                             Toggle to derive thermal power from flow + ΔT
                             instead of a dedicated heat meter (Track B).
Step 4 — Schedule          : Horizon length and optimisation tuning.

HA config-flow quirks applied throughout
-----------------------------------------
* Schema builders as methods (_schema_*): schemas are rebuilt dynamically on
  every render so that current values from _flow_data appear as defaults.
* _v() helper: resolves value as user_input > _flow_data > hardcoded default.
* _clear_absent_entity_keys(): HA/voluptuous silently drops absent Optional
  keys rather than sending None — this helper removes stale values that
  would otherwise survive a .update(user_input) unchanged.
* Re-render trick (step 3): when the "use_flow_sensors" toggle changes, the
  form is shown again (without saving) so the new sensor fields appear/hide.
  Users must press Submit twice: once to toggle, once to confirm.
* suggested_value for Optional entity selectors: preserves the existing
  entity ID without making the field appear "required".

Reconfigure flow
----------------
Mirrors the setup flow but calls async_update_reload_and_abort() instead of
async_create_entry(), and seeds _flow_data from the existing config entry.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_WEATHER_ENTITY,
    CONF_PRICE_SENSOR,
    CONF_HA_ENTITY_ID,
    CONF_TANK_TEMP_SENSOR,
    CONF_MIN_LWT,
    CONF_MAX_LWT,
    CONF_MAX_TANK_TEMP,
    CONF_HEAT_PUMP_OUTPUT_KW,
    CONF_MIN_OUTPUT_KW,
    CONF_TANK_VOLUME_L,
    CONF_LWT_STEP,
    CONF_TANK_STANDBY_LOSS_KWH,
    CONF_HORIZON_HOURS,
    CONF_START_PENALTY_KWH,
    CONF_LWT_HEATING_COLD,
    CONF_LWT_HEATING_MILD,
    CONF_T_ROOM,
    CONF_ELECTRICAL_ENERGY_SENSOR,
    CONF_THERMAL_POWER_SENSOR,
    CONF_USE_FLOW_SENSORS,
    CONF_FLOW_RATE_SENSOR,
    CONF_SUPPLY_TEMP_SENSOR,
    CONF_RETURN_TEMP_SENSOR,
    CONF_FLOW_UNIT,
    CONF_COMPRESSOR_FREQ_SENSOR,
    FLOW_UNIT_LMIN,
    FLOW_UNIT_M3H,
    DEFAULT_WEATHER_ENTITY,

    DEFAULT_TANK_TEMP_SENSOR,
    DEFAULT_MIN_LWT,
    DEFAULT_MAX_LWT,
    DEFAULT_MAX_TANK_TEMP,
    DEFAULT_HEAT_PUMP_OUTPUT_KW,
    DEFAULT_MIN_OUTPUT_KW,
    DEFAULT_TANK_VOLUME_L,
    DEFAULT_LWT_STEP,
    DEFAULT_TANK_STANDBY_LOSS_KWH,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_START_PENALTY_KWH,
    DEFAULT_FLOW_UNIT,
    DEFAULT_LWT_HEATING_COLD,
    DEFAULT_LWT_HEATING_MILD,
    DEFAULT_T_ROOM,
)

_LOGGER = logging.getLogger(__name__)


class HeatpumpMpcConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Four-step config flow for Heat Pump MPC."""

    VERSION = 1

    def __init__(self) -> None:
        self._flow_data: dict[str, Any] = {}
        self._entry = None  # populated during reconfigure

    # ------------------------------------------------------------------
    # Helpers (same patterns as Heating Analytics)
    # ------------------------------------------------------------------

    @staticmethod
    def _v(user_input, defaults, key, default=None):
        """Return value: user_input → _flow_data → hardcoded default."""
        if user_input and key in user_input:
            return user_input[key]
        if defaults and key in defaults:
            return defaults[key]
        return default

    def _clear_absent_entity_keys(self, user_input: dict, keys: list[str]) -> None:
        """
        Remove optional entity keys from _flow_data when the user cleared them.

        HA/voluptuous drops absent Optional keys from user_input entirely (no
        None) rather than sending None.  Without this, a previously saved
        entity ID survives .update(user_input) unchanged even though the user
        deliberately removed it.
        """
        for key in keys:
            if not user_input.get(key):
                self._flow_data.pop(key, None)

    def _needs_reload_learning(self, user_input: dict) -> bool:
        """
        Return True when the learning step must re-render to show/hide
        the flow-sensor fields.

        Called with the just-submitted user_input *before* saving it to
        _flow_data, so we compare the new toggle value against what fields
        are currently present in user_input.
        """
        use_flow = user_input.get(CONF_USE_FLOW_SENSORS, False)
        # Toggle just turned on — sensor fields not yet in the form.
        if use_flow and CONF_FLOW_RATE_SENSOR not in user_input:
            return True
        # Toggle just turned off — sensor fields still submitted.
        if not use_flow and CONF_FLOW_RATE_SENSOR in user_input:
            return True
        return False

    def _build_final_data(self) -> dict:
        """
        Normalise _flow_data before writing to the config entry.

        Strips falsy optional entity keys so EntitySelector never renders
        with a stale "None" value on reconfigure.
        """
        data = dict(self._flow_data)
        optional_entity_keys = [
            CONF_PRICE_SENSOR,
            CONF_HA_ENTITY_ID,
            CONF_THERMAL_POWER_SENSOR,
            CONF_ELECTRICAL_ENERGY_SENSOR,
            CONF_COMPRESSOR_FREQ_SENSOR,
            CONF_FLOW_RATE_SENSOR,
            CONF_SUPPLY_TEMP_SENSOR,
            CONF_RETURN_TEMP_SENSOR,
        ]
        for key in optional_entity_keys:
            if not data.get(key):
                data.pop(key, None)

        # If flow sensors are disabled, remove flow-sensor keys entirely
        # even if they were previously saved (user toggled Track B off).
        if not data.get(CONF_USE_FLOW_SENSORS):
            for key in (CONF_FLOW_RATE_SENSOR, CONF_SUPPLY_TEMP_SENSOR,
                        CONF_RETURN_TEMP_SENSOR, CONF_FLOW_UNIT):
                data.pop(key, None)

        return data

    # ------------------------------------------------------------------
    # Schema builders
    # ------------------------------------------------------------------

    def _schema_data_sources(self, user_input, defaults) -> vol.Schema:
        g = lambda k, d=None: self._v(user_input, defaults, k, d)
        schema: dict = {
            vol.Required(CONF_TANK_TEMP_SENSOR, default=g(CONF_TANK_TEMP_SENSOR, DEFAULT_TANK_TEMP_SENSOR)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Required(CONF_WEATHER_ENTITY, default=g(CONF_WEATHER_ENTITY, DEFAULT_WEATHER_ENTITY)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="weather")
            ),
        }
        # Optional — omit for COP-only optimisation (no price weighting).
        schema[vol.Optional(
            CONF_PRICE_SENSOR,
            description={"suggested_value": g(CONF_PRICE_SENSOR)},
        )] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
        # Optional — routes get_forecast to the right HA instance.
        schema[vol.Optional(
            CONF_HA_ENTITY_ID,
            description={"suggested_value": g(CONF_HA_ENTITY_ID)},
        )] = selector.EntitySelector(
            selector.EntitySelectorConfig(integration="heating_analytics")
        )
        return vol.Schema(schema)

    def _schema_heat_pump(self, user_input, defaults) -> vol.Schema:
        g = lambda k, d=None: self._v(user_input, defaults, k, d)
        return vol.Schema({
            vol.Required(CONF_HEAT_PUMP_OUTPUT_KW, default=g(CONF_HEAT_PUMP_OUTPUT_KW, DEFAULT_HEAT_PUMP_OUTPUT_KW)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1.0, max=30.0, step=0.1, unit_of_measurement="kW")
            ),
            vol.Required(CONF_MIN_OUTPUT_KW, default=g(CONF_MIN_OUTPUT_KW, DEFAULT_MIN_OUTPUT_KW)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.5, max=30.0, step=0.1, unit_of_measurement="kW")
            ),
            vol.Required(CONF_MIN_LWT, default=g(CONF_MIN_LWT, DEFAULT_MIN_LWT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=20.0, max=50.0, step=0.1, unit_of_measurement="°C")
            ),
            vol.Required(CONF_MAX_LWT, default=g(CONF_MAX_LWT, DEFAULT_MAX_LWT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=35.0, max=65.0, step=0.1, unit_of_measurement="°C")
            ),
            vol.Required(CONF_MAX_TANK_TEMP, default=g(CONF_MAX_TANK_TEMP, DEFAULT_MAX_TANK_TEMP)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=35.0, max=65.0, step=0.1, unit_of_measurement="°C")
            ),
            vol.Required(CONF_TANK_VOLUME_L, default=g(CONF_TANK_VOLUME_L, DEFAULT_TANK_VOLUME_L)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10.0, max=10000.0, step=10.0, unit_of_measurement="L")
            ),
            vol.Required(CONF_LWT_HEATING_COLD, default=g(CONF_LWT_HEATING_COLD, DEFAULT_LWT_HEATING_COLD)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=25.0, max=60.0, step=0.1, unit_of_measurement="°C")
            ),
            vol.Required(CONF_LWT_HEATING_MILD, default=g(CONF_LWT_HEATING_MILD, DEFAULT_LWT_HEATING_MILD)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=20.0, max=50.0, step=0.1, unit_of_measurement="°C")
            ),
            vol.Required(CONF_T_ROOM, default=g(CONF_T_ROOM, DEFAULT_T_ROOM)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=15.0, max=25.0, step=0.1, unit_of_measurement="°C")
            ),
        })

    def _schema_learning(self, user_input, defaults) -> vol.Schema:
        """
        Dynamic schema for the COP learning step.

        The "use_flow_sensors" boolean gates three additional entity selectors
        (flow rate, supply temp, return temp) plus a unit dropdown.
        When the toggle changes, _needs_reload_learning() causes a re-render
        so the fields appear / disappear without leaving the step.
        """
        g = lambda k, d=None: self._v(user_input, defaults, k, d)
        use_flow = g(CONF_USE_FLOW_SENSORS, False)

        schema: dict = {
            vol.Required(CONF_ELECTRICAL_ENERGY_SENSOR, default=g(CONF_ELECTRICAL_ENERGY_SENSOR, "")): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="energy")
            ),
        }
        # Optional direct thermal power sensor (Track A)
        schema[vol.Optional(
            CONF_THERMAL_POWER_SENSOR,
            description={"suggested_value": g(CONF_THERMAL_POWER_SENSOR)},
        )] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power")
        )
        # Compressor frequency from Modbus — enables capacity derating learning
        schema[vol.Optional(
            CONF_COMPRESSOR_FREQ_SENSOR,
            description={"suggested_value": g(CONF_COMPRESSOR_FREQ_SENSOR)},
        )] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
        # Toggle: derive power from flow × ΔT instead (Track B)
        schema[vol.Optional(CONF_USE_FLOW_SENSORS, default=use_flow)] = selector.BooleanSelector()

        if use_flow:
            schema[vol.Required(
                CONF_FLOW_RATE_SENSOR,
                default=g(CONF_FLOW_RATE_SENSOR, ""),
            )] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )
            schema[vol.Required(CONF_FLOW_UNIT, default=g(CONF_FLOW_UNIT, DEFAULT_FLOW_UNIT))] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[FLOW_UNIT_LMIN, FLOW_UNIT_M3H],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            schema[vol.Required(
                CONF_SUPPLY_TEMP_SENSOR,
                default=g(CONF_SUPPLY_TEMP_SENSOR, ""),
            )] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            )
            schema[vol.Required(
                CONF_RETURN_TEMP_SENSOR,
                default=g(CONF_RETURN_TEMP_SENSOR, ""),
            )] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            )

        return vol.Schema(schema)

    def _schema_schedule(self, user_input, defaults) -> vol.Schema:
        g = lambda k, d=None: self._v(user_input, defaults, k, d)
        return vol.Schema({
            vol.Required(CONF_HORIZON_HOURS, default=g(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=12, max=48, step=1, unit_of_measurement="h", mode="slider")
            ),
            vol.Required(CONF_LWT_STEP, default=g(CONF_LWT_STEP, DEFAULT_LWT_STEP)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1.0, max=10.0, step=1.0, unit_of_measurement="°C")
            ),
            vol.Required(CONF_TANK_STANDBY_LOSS_KWH, default=g(CONF_TANK_STANDBY_LOSS_KWH, DEFAULT_TANK_STANDBY_LOSS_KWH)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.0, max=0.5, step=0.01, unit_of_measurement="kWh/h")
            ),
            vol.Required(CONF_START_PENALTY_KWH, default=g(CONF_START_PENALTY_KWH, DEFAULT_START_PENALTY_KWH)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.0, max=0.5, step=0.01, unit_of_measurement="kWh", mode="slider")
            ),
        })

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_heat_pump(self, user_input: dict) -> dict[str, str]:
        errors: dict[str, str] = {}
        min_lwt = float(user_input[CONF_MIN_LWT])
        max_lwt = float(user_input[CONF_MAX_LWT])
        max_tank = float(user_input[CONF_MAX_TANK_TEMP])
        if max_lwt <= min_lwt:
            errors[CONF_MAX_LWT] = "max_lwt_below_min"
        elif max_tank < max_lwt:
            errors[CONF_MAX_TANK_TEMP] = "max_tank_below_max_lwt"
        lwt_cold = float(user_input[CONF_LWT_HEATING_COLD])
        lwt_mild = float(user_input[CONF_LWT_HEATING_MILD])
        if lwt_mild >= lwt_cold:
            errors[CONF_LWT_HEATING_MILD] = "lwt_mild_above_cold"
        return errors

    # ------------------------------------------------------------------
    # Setup flow
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Step 1: data sources."""
        if user_input is not None:
            self._flow_data.update(user_input)
            self._clear_absent_entity_keys(user_input, [CONF_PRICE_SENSOR, CONF_HA_ENTITY_ID])
            return await self.async_step_heat_pump()
        return self.async_show_form(
            step_id="user",
            data_schema=self._schema_data_sources(user_input, self._flow_data),
        )

    async def async_step_heat_pump(self, user_input=None) -> FlowResult:
        """Step 2: heat pump and tank parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate_heat_pump(user_input)
            if not errors:
                self._flow_data.update(user_input)
                return await self.async_step_learning()
        return self.async_show_form(
            step_id="heat_pump",
            data_schema=self._schema_heat_pump(user_input, self._flow_data),
            errors=errors,
        )

    async def async_step_learning(self, user_input=None) -> FlowResult:
        """
        Step 3: COP learning sensors.

        When the user flips "use_flow_sensors", the form re-renders to
        show or hide the flow/temperature sensor fields (Track B).
        """
        if user_input is not None:
            if self._needs_reload_learning(user_input):
                # Re-render without saving: user just toggled the switch.
                return self.async_show_form(
                    step_id="learning",
                    data_schema=self._schema_learning(user_input, self._flow_data),
                )
            self._flow_data.update(user_input)
            self._clear_absent_entity_keys(
                user_input,
                [CONF_THERMAL_POWER_SENSOR, CONF_COMPRESSOR_FREQ_SENSOR,
                 CONF_FLOW_RATE_SENSOR, CONF_SUPPLY_TEMP_SENSOR, CONF_RETURN_TEMP_SENSOR],
            )
            return await self.async_step_schedule()
        return self.async_show_form(
            step_id="learning",
            data_schema=self._schema_learning(None, self._flow_data),
        )

    async def async_step_schedule(self, user_input=None) -> FlowResult:
        """Step 4: optimisation tuning."""
        if user_input is not None:
            self._flow_data.update(user_input)
            data = self._build_final_data()
            ha_entity = data.get(CONF_HA_ENTITY_ID, "")
            title = f"Heat Pump MPC ({ha_entity})" if ha_entity else "Heat Pump MPC"
            return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="schedule",
            data_schema=self._schema_schedule(None, self._flow_data),
        )

    # ------------------------------------------------------------------
    # Reconfigure flow
    # ------------------------------------------------------------------

    async def async_step_reconfigure(self, user_input=None) -> FlowResult:
        """Step 1 (reconfigure): data sources."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if user_input is None:
            self._entry = entry
            self._flow_data = {**entry.data}
        if user_input is not None:
            self._flow_data.update(user_input)
            self._clear_absent_entity_keys(user_input, [CONF_PRICE_SENSOR, CONF_HA_ENTITY_ID])
            return await self.async_step_reconfigure_heat_pump()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema_data_sources(user_input, self._flow_data),
        )

    async def async_step_reconfigure_heat_pump(self, user_input=None) -> FlowResult:
        """Step 2 (reconfigure): heat pump and tank parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate_heat_pump(user_input)
            if not errors:
                self._flow_data.update(user_input)
                return await self.async_step_reconfigure_learning()
        return self.async_show_form(
            step_id="reconfigure_heat_pump",
            data_schema=self._schema_heat_pump(user_input, self._flow_data),
            errors=errors,
        )

    async def async_step_reconfigure_learning(self, user_input=None) -> FlowResult:
        """Step 3 (reconfigure): COP learning sensors."""
        if user_input is not None:
            if self._needs_reload_learning(user_input):
                return self.async_show_form(
                    step_id="reconfigure_learning",
                    data_schema=self._schema_learning(user_input, self._flow_data),
                )
            self._flow_data.update(user_input)
            self._clear_absent_entity_keys(
                user_input,
                [CONF_THERMAL_POWER_SENSOR, CONF_COMPRESSOR_FREQ_SENSOR,
                 CONF_FLOW_RATE_SENSOR, CONF_SUPPLY_TEMP_SENSOR, CONF_RETURN_TEMP_SENSOR],
            )
            return await self.async_step_reconfigure_schedule()
        return self.async_show_form(
            step_id="reconfigure_learning",
            data_schema=self._schema_learning(None, self._flow_data),
        )

    async def async_step_reconfigure_schedule(self, user_input=None) -> FlowResult:
        """Step 4 (reconfigure): optimisation tuning."""
        if user_input is not None:
            self._flow_data.update(user_input)
            return self.async_update_reload_and_abort(
                self._entry,
                data=self._build_final_data(),
            )
        return self.async_show_form(
            step_id="reconfigure_schedule",
            data_schema=self._schema_schedule(None, self._flow_data),
        )
