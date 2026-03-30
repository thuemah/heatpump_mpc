"""
Coordinator for the Heat Pump MPC integration.

Responsibilities
----------------
1. Call ``heating_analytics.get_forecast`` to obtain per-hour house demand
   (kWh) and outdoor temperature directly from the Heating Analytics model.
2. Read hourly electricity prices from a Nordpool/Tibber sensor.
3. Call ``weather.get_forecasts`` for relative humidity (needed for the
   defrost penalty in the COP model).
4. Read the current buffer-tank temperature from a dedicated sensor.
5. Run ``MpcSolver.solve()`` and store the result so sensor entities can
   expose it to Home Assistant.

Multi-instance
--------------
Each config entry owns exactly one coordinator instance.  The
``CONF_HA_ENTITY_ID`` setting carries the entity ID of *any* sensor that
belongs to the target Heating Analytics instance; this is forwarded as
``entity_id`` in the ``get_forecast`` service call so HA can route it to
the correct coordinator when multiple HA instances are installed.

Nothing here touches the MPC maths — that lives in ``core/``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
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
    CONF_FLOW_UNIT,
    CONF_SUPPLY_TEMP_SENSOR,
    CONF_RETURN_TEMP_SENSOR,
    CONF_DHW_ENABLED,
    CONF_DHW_TEMP_SENSOR,
    CONF_DHW_OPERATION_SENSOR,
    CONF_DHW_TANK_VOLUME_L,
    CONF_DHW_MIN_TEMP,
    CONF_DHW_TARGET_TEMP,
    CONF_DHW_LWT,
    CONF_DHW_DAILY_DEMAND_KWH,
    CONF_DHW_READY_TIMES,
    DEFAULT_WEATHER_ENTITY,
    DEFAULT_PRICE_SENSOR,
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
    DEFAULT_TANK_TEMP,
    DEFAULT_RH,
    DEFAULT_UNIFORM_PRICE,
    DEFAULT_DHW_TANK_VOLUME_L,
    DEFAULT_DHW_MIN_TEMP,
    DEFAULT_DHW_TARGET_TEMP,
    DEFAULT_DHW_LWT,
    DEFAULT_DHW_DAILY_DEMAND_KWH,
    DEFAULT_DHW_TANK_TEMP,
    HEATING_CURVE_T_COLD,
    HEATING_CURVE_T_MILD,
    DEFAULT_LWT_HEATING_COLD,
    DEFAULT_LWT_HEATING_MILD,
    DEFAULT_T_ROOM,
    FLOW_UNIT_LMIN,
    UPDATE_INTERVAL_MINUTES,
    HA_DOMAIN,
    HA_SERVICE_GET_FORECAST,
    DOMAIN,
    RESULT_OPTIMAL_LWT,
    RESULT_OPTIMAL_OUTPUT_KW,
    RESULT_TOTAL_COST,
    RESULT_FEASIBLE,
    RESULT_SCHEDULE,
    RESULT_CURRENT_COP,
    RESULT_NEXT_RUN_START,
    RESULT_PUMP_ON_NOW,
    RESULT_PLANNED_RUN_HOURS,
    RESULT_PLANNED_STARTS,
    RESULT_PLANNED_KWH_THERMAL,
    RESULT_PLANNED_KWH_ELECTRICAL,
    RESULT_DHW_ON_NOW,
    RESULT_DHW_SETPOINT,
    RESULT_DHW_PLANNED_HOURS,
    RESULT_SH_THERMAL_ENERGY_TOTAL_KWH,
    CONF_RATED_MAX_ELEC_KW,
)
from .core.cop_learner import CopLearner, CopLearnerState, CopObservation
from .core.heat_pump_model import HeatPumpModel
from .core.mpc_solver import HorizonPoint, MpcConfig, MpcResult, MpcSolver
from .storage import HeatpumpMpcStorage

_LOGGER = logging.getLogger(__name__)


class HeatpumpMpcCoordinator(DataUpdateCoordinator):
    """
    Fetches external data and runs the MPC solver every 30 minutes.

    ``coordinator.data`` is a plain dict that sensor entities read from.
    All keys are defined in ``const.py`` as ``RESULT_*`` constants.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self._model = HeatPumpModel()
        self._solver = MpcSolver(self._model)
        self._storage = HeatpumpMpcStorage(hass, entry.entry_id)

        # COP / capacity learner — state loaded from disk in async_setup().
        # Falls back to datasheet defaults if nothing has been persisted yet.
        self._learner = CopLearner(CopLearnerState())

        # Previous cumulative electrical energy reading (kWh) for delta computation.
        self._prev_elec_kwh: float | None = None
        self._prev_update_time: datetime | None = None
        # Previous MPC decisions and tank state — used to build COP / capacity
        # observations for the next learning cycle.
        self._prev_output_kw: float | None = None
        self._prev_tank_temp: float | None = None
        self._prev_optimal_lwt: float | None = None
        # DHW state tracking — used for COP contamination filter.
        self._prev_dhw_temp: float | None = None
        # One-shot guard so the missing-rated_max_elec warning is logged only once.
        self._logged_missing_rated_max_elec: bool = False

        # SH thermal energy accumulation (Track C)
        # _sh_total_kwh_th: lifetime cumulative SH thermal kWh (total_increasing sensor)
        # _sh_hourly_buffer: rolling list of completed-hour records for get_sh_hourly service
        # _sh_hour_start: datetime of the hour currently being accumulated
        # _sh_current_hour_kwh: thermal kWh accumulator for the in-progress hour
        # _sh_current_hour_kwh_el: electrical kWh accumulator for SH windows in the in-progress hour
        # _sh_current_hour_sh_windows: count of 30-min SH windows in the in-progress hour
        # _sh_current_hour_dhw_windows: count of 30-min DHW windows in the in-progress hour
        self._sh_total_kwh_th: float = 0.0
        self._sh_hourly_buffer: list[dict] = []
        self._sh_hour_start: datetime | None = None
        self._sh_current_hour_kwh: float = 0.0
        self._sh_current_hour_kwh_el: float = 0.0
        self._sh_current_hour_sh_windows: int = 0
        self._sh_current_hour_dhw_windows: int = 0

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """
        Load persisted learner and SH state from storage.

        Must be awaited before the first :py:meth:`_async_update_data` call
        so that learned COP / capacity parameters and SH totals are available
        immediately rather than reverting to cold-start defaults after every
        HA restart.
        """
        state = await self._storage.async_load_learner_state()
        self._learner = CopLearner(state)
        _LOGGER.debug(
            "Learner state loaded: η_Carnot=%.4f f_defrost=%.4f clean_samples=%d",
            state.eta_carnot,
            state.f_defrost,
            state.eta_carnot_samples,
        )

        sh_total, sh_buffer = await self._storage.async_load_sh_state()
        self._sh_total_kwh_th = sh_total
        self._sh_hourly_buffer = sh_buffer

    # ------------------------------------------------------------------
    # DataUpdateCoordinator protocol
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        """Fetch all data sources, run the solver, return a result dict."""
        try:
            now = dt_util.now()

            ha_forecast, weather_forecast, raw_prices = await asyncio.gather(
                self._async_get_ha_forecast(),
                self._async_get_weather_forecast(),
                self._async_get_prices(),
            )

            tank_temp = self._get_tank_temp()
            if tank_temp is None:
                raise UpdateFailed(
                    "Buffer tank temperature sensor is unavailable — "
                    "solver run aborted to avoid a fictitious schedule."
                )

            dhw_tank_temp = self._get_dhw_tank_temp()

            # Build RH lookup from weather entity (hour → humidity %).
            rh_map = _build_rh_map(weather_forecast)

            horizon = self._build_horizon(ha_forecast, rh_map, raw_prices)

            if not horizon:
                raise UpdateFailed(
                    "Cannot build optimisation horizon: "
                    "no overlapping Heating Analytics, weather and price data."
                )

            k_emission = self._compute_k_emission(horizon)
            dhw_enabled = bool(self.entry.data.get(CONF_DHW_ENABLED, False))
            if dhw_enabled and horizon:
                horizon_start = now.replace(minute=0, second=0, microsecond=0)
                ready_by_hours = self._compute_ready_by_indices(horizon_start, len(horizon))
            else:
                ready_by_hours = []
            config = self._build_mpc_config(
                k_emission=k_emission, dhw_ready_by_hours=ready_by_hours
            )

            # Learn from real measurements taken since the last update, then
            # update the capacity curve in the model before running the solver.
            await self._learn_from_sensors(now, horizon, config, dhw_tank_temp)
            self._model.apply_learned_capacity(self._learner.get_capacity_anchors())

            result: MpcResult = self._solver.solve(
                horizon, tank_temp, config, dhw_tank_temp_init=dhw_tank_temp
            )

            self._prev_update_time = now
            self._prev_output_kw = result.optimal_output_kw
            self._prev_tank_temp = tank_temp
            self._prev_optimal_lwt = result.optimal_lwt
            self._prev_dhw_temp = dhw_tank_temp

            result_dict = _result_to_dict(result)
            result_dict[RESULT_SH_THERMAL_ENERGY_TOTAL_KWH] = round(
                self._sh_total_kwh_th, 4
            )
            return result_dict

        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Unexpected MPC update error: {err}") from err

    # ------------------------------------------------------------------
    # Data-source helpers
    # ------------------------------------------------------------------

    async def _async_get_ha_forecast(self) -> list[dict]:
        """
        Call ``heating_analytics.get_forecast`` and return the hourly plan.

        Each item contains at minimum:
        - ``datetime``: ISO string
        - ``kwh``: predicted house demand for that hour
        - ``temp``: outdoor temperature (°C)

        The optional ``entity_id`` routes the call to the correct HA instance
        when multiple Heating Analytics instances are installed.

        Returns an empty list on any failure so the caller can handle
        degraded state gracefully.
        """
        ha_entity_id: str | None = self.entry.data.get(CONF_HA_ENTITY_ID)
        horizon_hours: int = int(
            self.entry.data.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS)
        )
        # HA's service takes whole days; round up to cover the horizon.
        days = max(1, -(-horizon_hours // 24))  # ceiling division

        service_data: dict = {"days": days}
        if ha_entity_id:
            service_data["entity_id"] = ha_entity_id

        try:
            response = await self.hass.services.async_call(
                HA_DOMAIN,
                HA_SERVICE_GET_FORECAST,
                service_data,
                blocking=True,
                return_response=True,
            )
            plan: list[dict] = response.get("forecast", [])
            _LOGGER.debug(
                "Heating Analytics get_forecast returned %d hourly slots.", len(plan)
            )
            return plan
        except Exception as err:
            _LOGGER.warning("Failed to call heating_analytics.get_forecast: %s", err)
            return []

    async def _async_get_weather_forecast(self) -> list[dict]:
        """
        Fetch the hourly weather forecast from the configured HA weather entity.

        Only used for relative humidity — temperature and demand come from
        Heating Analytics.  Returns an empty list on failure; the horizon
        builder will fall back to ``DEFAULT_RH`` for missing slots.
        """
        entity_id = self.entry.data.get(CONF_WEATHER_ENTITY, DEFAULT_WEATHER_ENTITY)
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            forecasts: list[dict] = (
                response.get(entity_id, {}).get("forecast", [])
            )
            _LOGGER.debug(
                "Weather entity %s returned %d hourly slots (for RH).",
                entity_id,
                len(forecasts),
            )
            return forecasts
        except Exception as err:
            _LOGGER.warning(
                "Failed to get weather forecast from %s: %s — "
                "defrost RH will use default %.0f%%.",
                entity_id,
                err,
                DEFAULT_RH,
            )
            return []

    async def _async_get_prices(self) -> list[dict]:
        """
        Read hourly electricity prices from the configured sensor.

        Supports Nordpool format: ``raw_today`` and ``raw_tomorrow`` are lists
        of ``{"start": "<ISO>", "value": <NOK/kWh>}``.

        Returns a flat, time-ordered list spanning today and tomorrow (when
        available).  Returns an empty list when no sensor is configured or on
        failure; the horizon builder will then use a uniform price of
        ``DEFAULT_UNIFORM_PRICE`` so the solver optimises for COP only.
        """
        entity_id: str | None = self.entry.data.get(CONF_PRICE_SENSOR)
        if not entity_id:
            _LOGGER.debug(
                "No price sensor configured — optimising for COP only "
                "(uniform price %.2f).",
                DEFAULT_UNIFORM_PRICE,
            )
            return []

        state = self.hass.states.get(entity_id)

        if state is None:
            _LOGGER.warning("Price sensor not found: %s", entity_id)
            return []

        attrs = state.attributes
        raw_today: list[dict] = attrs.get("raw_today", [])
        raw_tomorrow: list[dict] = attrs.get("raw_tomorrow", [])

        _LOGGER.debug(
            "Price sensor %s: %d today + %d tomorrow slots.",
            entity_id,
            len(raw_today),
            len(raw_tomorrow),
        )
        return list(raw_today) + list(raw_tomorrow)

    def _get_tank_temp(self) -> float | None:
        """
        Read the current buffer tank temperature (°C).

        Returns ``None`` when the sensor is unavailable or reports an
        unreadable state (``unknown`` / ``unavailable``).  The caller is
        responsible for aborting the solver run in that case so that a
        fictitious schedule based on a stale default is never produced.
        """
        entity_id = self.entry.data.get(CONF_TANK_TEMP_SENSOR, DEFAULT_TANK_TEMP_SENSOR)
        state = self.hass.states.get(entity_id)

        if state is None:
            # Entity not yet registered in the state machine — normal during HA
            # startup while other integrations are still loading.  Log at DEBUG
            # to avoid alarming log noise; the coordinator will retry on the
            # next scheduled interval once all entities have settled.
            _LOGGER.debug(
                "Tank temperature sensor %s not yet registered "
                "(HA may still be starting) — aborting solver run.",
                entity_id,
            )
            return None

        if state.state in ("unknown", "unavailable", ""):
            # Entity is registered but actively reporting a bad state — this is
            # a genuine sensor fault worth surfacing at WARNING level.
            _LOGGER.warning(
                "Tank temperature sensor %s is %r — aborting solver run to avoid "
                "generating a fictitious schedule.",
                entity_id,
                state.state,
            )
            return None

        try:
            return float(state.state)
        except ValueError:
            _LOGGER.warning(
                "Cannot parse tank temperature '%s' from %s — aborting solver run.",
                state.state,
                entity_id,
            )
            return None

    def _get_dhw_tank_temp(self) -> float | None:
        """
        Read the current DHW tank temperature (°C).

        Returns None when DHW is disabled or the sensor is unavailable.
        Falls back to ``DEFAULT_DHW_TANK_TEMP`` when the sensor is
        configured but temporarily unavailable.
        """
        if not self.entry.data.get(CONF_DHW_ENABLED):
            return None

        entity_id = self.entry.data.get(CONF_DHW_TEMP_SENSOR)
        if not entity_id:
            return None

        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            _LOGGER.debug(
                "DHW tank temperature sensor not available (%s); using default %.1f °C.",
                entity_id,
                DEFAULT_DHW_TANK_TEMP,
            )
            return DEFAULT_DHW_TANK_TEMP

        try:
            return float(state.state)
        except ValueError:
            _LOGGER.warning(
                "Cannot parse DHW tank temp '%s' from %s; using default.",
                state.state,
                entity_id,
            )
            return DEFAULT_DHW_TANK_TEMP

    def _read_dhw_operation_sensor(self) -> bool:
        """Return True when the DHW operation sensor reports the HP is in DHW mode.

        Returns False when the sensor is not configured, unavailable, or "off".
        Treats any state other than "on" as inactive so that transient
        "unavailable" states do not accidentally suppress COP observations.
        """
        entity_id = self.entry.data.get(CONF_DHW_OPERATION_SENSOR)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return False
        return state.state == "on"

    # ------------------------------------------------------------------
    # SH thermal energy accumulation helpers (Track C)
    # ------------------------------------------------------------------

    @property
    def sh_hourly_buffer(self) -> list[dict]:
        """Rolling buffer of completed per-hour SH thermal energy records.

        Each entry contains:
        - ``datetime``:  ISO timestamp of the hour start.
        - ``kwh_th_sh``: Thermal kWh delivered for space heating (0.0 during DHW/off hours).
        - ``kwh_el_sh``: Electrical kWh consumed during SH windows in this hour.
          Heating Analytics can derive average COP as ``kwh_th_sh / kwh_el_sh``.
        - ``mode``:      ``"sh"`` when SH windows dominated, ``"dhw"`` when DHW
          windows dominated, ``"off"`` when neither recorded any consumption.

        The buffer holds up to 48 entries (two days).
        """
        return self._sh_hourly_buffer

    def _finalize_sh_hour_if_needed(self, now: datetime) -> None:
        """Flush the current SH accumulator to the buffer when the clock hour turns.

        Called at the start of every :py:meth:`_learn_from_sensors` invocation
        so that hour boundaries are always detected, even when electrical data
        is unavailable or the DHW filter is active.

        On the very first call (``_sh_hour_start is None``) the method simply
        records the current hour as the start of tracking without flushing.
        """
        now_hour_start = now.replace(minute=0, second=0, microsecond=0)

        if self._sh_hour_start is None:
            # First call — initialise tracking; nothing to flush yet.
            self._sh_hour_start = now_hour_start
            return

        if now_hour_start <= self._sh_hour_start:
            # Still within the same hour — nothing to do.
            return

        # Clock has advanced to a new hour; finalise the previous one.
        if self._sh_current_hour_sh_windows > self._sh_current_hour_dhw_windows:
            mode = "sh"
        elif self._sh_current_hour_dhw_windows > self._sh_current_hour_sh_windows:
            mode = "dhw"
        else:
            mode = "off"

        entry: dict = {
            "datetime": self._sh_hour_start.isoformat(),
            "kwh_th_sh": round(self._sh_current_hour_kwh, 4),
            "kwh_el_sh": round(self._sh_current_hour_kwh_el, 4),
            "mode": mode,
        }
        self._sh_hourly_buffer.append(entry)
        if len(self._sh_hourly_buffer) > 48:
            self._sh_hourly_buffer = self._sh_hourly_buffer[-48:]

        _LOGGER.debug(
            "SH hourly: finalized %s → %.4f kWh_th %.4f kWh_el mode=%s (buffer size=%d)",
            self._sh_hour_start.isoformat(),
            self._sh_current_hour_kwh,
            self._sh_current_hour_kwh_el,
            mode,
            len(self._sh_hourly_buffer),
        )

        self._sh_current_hour_kwh = 0.0
        self._sh_current_hour_kwh_el = 0.0
        self._sh_current_hour_sh_windows = 0
        self._sh_current_hour_dhw_windows = 0
        self._sh_hour_start = now_hour_start

        # Persist so the buffer survives HA restarts.
        self._storage.schedule_full_save(
            self._learner.state,
            self._sh_total_kwh_th,
            self._sh_hourly_buffer,
        )

    # ------------------------------------------------------------------
    # COP / capacity learning helpers
    # ------------------------------------------------------------------

    async def _learn_from_sensors(
        self,
        now: datetime,
        horizon: list[HorizonPoint],
        config: MpcConfig,
        dhw_tank_temp_now: float | None = None,
    ) -> None:
        """
        Attempt to build a :class:`CopObservation` from current sensor readings
        and submit it to the learner.  Also accumulates SH thermal energy for
        the ``get_sh_hourly`` service and the ``total_increasing`` energy sensor.

        Called every coordinator update (every 30 minutes).  All sensor reads
        degrade gracefully: missing sensors are skipped rather than raising.

        The observation window spans from ``_prev_update_time`` to ``now``.
        On the first update there is no baseline for the electrical energy delta,
        so we record the current cumulative value and skip this cycle.

        DHW contamination filter
        ~~~~~~~~~~~~~~~~~~~~~~~~
        When a DHW temp sensor is configured and the DHW tank temperature rose
        by more than 0.5 °C since the last cycle, the heat pump was likely
        running in DHW mode (high LWT).  COP observations taken during DHW mode
        would contaminate the SH COP model, so the cycle is skipped and no SH
        energy is accumulated for that window.

        SH thermal energy accumulation
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        For every SH window, ``kWh_th_sh = COP_sh × Δ_kWh_el`` is accumulated
        into the current-hour bucket.  Completed hours are flushed into the
        rolling ``_sh_hourly_buffer``.  The electrical energy sensor is the only
        hard dependency; the heat meter / flow sensors are NOT required here
        (they are used only for COP learning, below).
        """
        d = self.entry.data

        # --- Hour-boundary finalization (always, before any early return) ---
        self._finalize_sh_hour_if_needed(now)

        # --- DHW contamination filter ---
        # Priority 1: direct operation sensor — reliable, catches DHW mode even
        # when the tank is near target temperature (small or zero thermal rise).
        # Priority 2: temperature-rise heuristic — fallback when no sensor is
        # configured (existing behaviour, unchanged).
        _dhw_skip = False
        if self._read_dhw_operation_sensor():
            _LOGGER.debug(
                "DHW contamination filter: operation sensor ON "
                "— skipping COP observation (HP running in DHW mode).",
            )
            _dhw_skip = True
        elif dhw_tank_temp_now is not None and self._prev_dhw_temp is not None:
            dhw_rise = dhw_tank_temp_now - self._prev_dhw_temp
            if dhw_rise > 0.5:
                _LOGGER.debug(
                    "DHW contamination filter: DHW tank rose %.1f °C (%.1f → %.1f °C) "
                    "— skipping COP observation (HP likely ran in DHW mode).",
                    dhw_rise,
                    self._prev_dhw_temp,
                    dhw_tank_temp_now,
                )
                _dhw_skip = True
        if _dhw_skip:
            # Count this 30-min slot as a DHW window for mode determination.
            self._sh_current_hour_dhw_windows += 1
            # Still advance the electrical baseline so the next cycle
            # uses a clean delta window.  No SH energy accumulated for this window.
            elec_kwh_baseline = self._read_float_state(
                d.get(CONF_ELECTRICAL_ENERGY_SENSOR)
            )
            if elec_kwh_baseline is not None:
                self._prev_elec_kwh = elec_kwh_baseline
            return

        # --- Electrical energy delta ---
        elec_kwh_now = self._read_float_state(d.get(CONF_ELECTRICAL_ENERGY_SENSOR))
        if elec_kwh_now is None:
            return

        if self._prev_elec_kwh is None or self._prev_update_time is None:
            # First cycle — record baseline, nothing to learn yet.
            self._prev_elec_kwh = elec_kwh_now
            return

        elec_delta = elec_kwh_now - self._prev_elec_kwh
        if elec_delta <= 0.0:
            # Counter reset or pump was off — skip but still update baseline.
            self._prev_elec_kwh = elec_kwh_now
            return

        duration_h = (now - self._prev_update_time).total_seconds() / 3600.0
        if duration_h < 0.1:
            return  # Unexpectedly short window; skip.

        # --- Contextual conditions from current horizon slot ---
        # Computed here (before the thermal-output check) so the SH accumulation
        # below works even when no heat meter is configured.
        t_outdoor = horizon[0].t_outdoor if horizon else 5.0
        rh = horizon[0].rh if horizon else DEFAULT_RH
        # Use the LWT the MPC actually recommended last cycle; fall back to
        # min_lwt only on the very first cycle before any solve has run.
        lwt = (
            self._prev_optimal_lwt
            if self._prev_optimal_lwt is not None
            else float(d.get(CONF_MIN_LWT, DEFAULT_MIN_LWT))
        )

        # --- SH thermal energy accumulation (Track C) ---
        # kWh_th_sh = COP_sh × Δ_kWh_el  — no heat meter required.
        cop_sh = max(1.0, self._model.get_effective_cop(t_outdoor, rh, lwt))
        sh_kwh = cop_sh * elec_delta
        self._sh_current_hour_sh_windows += 1
        self._sh_current_hour_kwh += sh_kwh
        self._sh_current_hour_kwh_el += elec_delta
        self._sh_total_kwh_th += sh_kwh
        _LOGGER.debug(
            "SH accumulation: cop_sh=%.2f elec_delta=%.4f kWh → sh=%.4f kWh "
            "(hour_th=%.4f hour_el=%.4f total=%.3f)",
            cop_sh,
            elec_delta,
            sh_kwh,
            self._sh_current_hour_kwh,
            self._sh_current_hour_kwh_el,
            self._sh_total_kwh_th,
        )

        # --- Thermal output (for COP learning only) ---
        heat_kw = self._read_thermal_kw(d)
        if heat_kw is None:
            # No heat meter available — SH energy already accumulated above;
            # schedule a save and exit (COP learning skipped this cycle).
            self._prev_elec_kwh = elec_kwh_now
            self._storage.schedule_full_save(
                self._learner.state,
                self._sh_total_kwh_th,
                self._sh_hourly_buffer,
            )
            return
        heat_kwh = heat_kw * duration_h

        # --- Tank headroom at start of window (filters tank-limited observations) ---
        tank_headroom_kwh: float | None = None
        if self._prev_tank_temp is not None:
            _kwh_per_k = config.tank_volume_liters * 1.16e-3
            tank_energy = max(0.0, (self._prev_tank_temp - config.min_lwt) * _kwh_per_k)
            max_energy = (config.max_tank_temp - config.min_lwt) * _kwh_per_k
            tank_headroom_kwh = max(0.0, max_energy - tank_energy)

        d = self.entry.data
        rated_max_elec_raw = d.get(CONF_RATED_MAX_ELEC_KW)
        rated_max_elec_kw: float | None = None
        if rated_max_elec_raw is not None:
            rated_max_elec_kw = float(rated_max_elec_raw)
        elif not self._logged_missing_rated_max_elec:
            _LOGGER.warning(
                "rated_max_elec_kw is not configured — capacity learning is "
                "disabled. Please reconfigure the integration and set 'Rated "
                "Max Electrical Power' from the heat pump datasheet."
            )
            self._logged_missing_rated_max_elec = True

        obs = CopObservation(
            t_outdoor=t_outdoor,
            rh=rh,
            lwt=lwt,
            heat_out_kwh=heat_kwh,
            elec_kwh=elec_delta,
            duration_hours=duration_h,
            rated_max_elec_kw=rated_max_elec_kw,
            rated_kw=config.heat_pump_output_kw,
            tank_headroom_kwh=tank_headroom_kwh,
        )

        result = self._learner.observe(obs)
        _LOGGER.debug(
            "COP learning: accepted=%s cop_measured=%s eta_carnot=%.4f "
            "capacity_updated=%s anchor=%.0f frac=%.3f",
            result.accepted,
            result.cop_measured,
            result.eta_carnot_after,
            result.capacity_updated,
            result.capacity_anchor_c or 0,
            result.capacity_frac_observed or 0,
        )

        # Persist learner + SH state (debounced — multiple rapid calls coalesce).
        self._storage.schedule_full_save(
            self._learner.state,
            self._sh_total_kwh_th,
            self._sh_hourly_buffer,
        )

        self._prev_elec_kwh = elec_kwh_now

    def _read_thermal_kw(self, d: dict) -> float | None:
        """
        Return current thermal power in kW from whichever source is configured.

        Track A: direct ``thermal_power_sensor`` (instantaneous kW).
        Track B: ``flow_rate_sensor`` × ΔT × conversion factor.

        Returns None when no thermal measurement is available.
        """
        # Track A — dedicated heat meter
        power_kw = self._read_float_state(d.get(CONF_THERMAL_POWER_SENSOR))
        if power_kw is not None:
            return power_kw

        # Track B — flow × ΔT
        if not d.get(CONF_USE_FLOW_SENSORS):
            return None

        flow = self._read_float_state(d.get(CONF_FLOW_RATE_SENSOR))
        supply = self._read_float_state(d.get(CONF_SUPPLY_TEMP_SENSOR))
        ret = self._read_float_state(d.get(CONF_RETURN_TEMP_SENSOR))

        if flow is None or supply is None or ret is None:
            return None

        delta_t = supply - ret
        if delta_t <= 0.0:
            return None

        flow_unit = d.get(CONF_FLOW_UNIT, FLOW_UNIT_LMIN)
        # Convert to L/min if necessary, then apply P[kW] = Q[L/min] × ΔT × 1.163/60
        if flow_unit != FLOW_UNIT_LMIN:
            flow = flow * 1000.0 / 60.0   # m³/h → L/min

        return flow * delta_t * 1.163 / 60.0

    def _read_float_state(self, entity_id: str | None) -> float | None:
        """
        Read a sensor entity state and return it as a float, or None on any failure.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Horizon builder
    # ------------------------------------------------------------------

    def _build_horizon(
        self,
        ha_forecast: list[dict],
        rh_map: dict[datetime, float],
        raw_prices: list[dict],
    ) -> list[HorizonPoint]:
        """
        Merge Heating Analytics forecast, weather humidity, and price data
        into an ordered list of ``HorizonPoint`` objects.

        Data sources per slot:
        - ``house_demand``: HA electrical kWh converted to **thermal** kWh
          by multiplying by our COP model at the heating-curve-prescribed LWT
          for that outdoor temperature.  Heating Analytics predicts electrical
          consumption; the solver and tank simulation require thermal energy.
        - ``t_outdoor``:    ``temp`` from Heating Analytics (inertia-adjusted)
        - ``rh``:           humidity from HA weather entity (or DEFAULT_RH)
        - ``price``:        Nordpool/Tibber price for the matching hour

        Slots are included only if both HA forecast AND price data exist for
        that hour.  The horizon is capped at ``CONF_HORIZON_HOURS``.
        """
        horizon_hours: int = int(
            self.entry.data.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS)
        )

        # Heating curve config — used to derive the COP reference for each slot.
        lwt_cold = float(self.entry.data.get(CONF_LWT_HEATING_COLD, DEFAULT_LWT_HEATING_COLD))
        lwt_mild = float(self.entry.data.get(CONF_LWT_HEATING_MILD, DEFAULT_LWT_HEATING_MILD))

        # DHW hourly demand — evenly distributed daily budget.
        dhw_enabled: bool = bool(self.entry.data.get(CONF_DHW_ENABLED, False))
        dhw_hourly_demand = (
            float(self.entry.data.get(CONF_DHW_DAILY_DEMAND_KWH, DEFAULT_DHW_DAILY_DEMAND_KWH))
            / 24.0
            if dhw_enabled
            else 0.0
        )

        price_sensor: str | None = self.entry.data.get(CONF_PRICE_SENSOR)
        price_map = _parse_price_map(raw_prices)

        if price_sensor and not price_map:
            _LOGGER.warning(
                "Price sensor %s is configured but returned no data — "
                "cannot build horizon.",
                price_sensor,
            )
            return []

        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        horizon: list[HorizonPoint] = []

        for slot in ha_forecast:
            if len(horizon) >= horizon_hours:
                break

            slot_dt = _parse_slot_dt(slot.get("datetime", ""))
            if slot_dt is None or slot_dt < now:
                continue

            price = _lookup_price(price_map, slot_dt)
            if price is None:
                if price_sensor:
                    # Sensor is configured but this hour has no price — stop here.
                    _LOGGER.debug(
                        "No price for %s; truncating horizon at %d hours.",
                        slot_dt.isoformat(),
                        len(horizon),
                    )
                    break
                # No sensor configured: optimise for COP only.
                price = DEFAULT_UNIFORM_PRICE

            t_out = float(slot.get("temp", 5.0))
            rh = rh_map.get(slot_dt, DEFAULT_RH)

            # Heating Analytics delivers electrical kWh.  Convert to thermal kWh
            # using our COP model at the heating-curve-prescribed LWT for this
            # outdoor temperature.  This is the same reference the heating curve
            # was designed around, so the conversion is self-consistent.
            # All downstream quantities (tank simulation, emission constraint) are
            # purely thermal; electricity only re-enters through cost = thermal / COP.
            lwt_ref = _heating_curve_lwt(t_out, lwt_cold, lwt_mild)
            ref_cop = max(1.0, self._model.get_effective_cop(t_out, rh, lwt_ref))
            thermal_demand = float(slot.get("kwh", 0.0)) * ref_cop

            horizon.append(
                HorizonPoint(
                    price=price,
                    t_outdoor=t_out,
                    rh=rh,
                    house_demand=thermal_demand,
                    dhw_demand=dhw_hourly_demand,
                )
            )

        _LOGGER.debug("Built horizon with %d hours.", len(horizon))
        return horizon

    # ------------------------------------------------------------------
    # Emission coefficient helper
    # ------------------------------------------------------------------

    def _compute_k_emission(self, horizon: list[HorizonPoint]) -> float:
        """
        Back-calculate the emission system's thermal transfer coefficient k
        (kW per K above t_room) from the already-converted thermal horizon.

        ``horizon[t].house_demand`` is already thermal kWh (converted from
        Heating Analytics' electrical forecast in ``_build_horizon``).  For
        each slot the heating curve prescribes the target LWT at that outdoor
        temperature; the emission system must satisfy:

            Q_thermal = k × (LWT_curve(T_outdoor) − T_room)   [kWh_th / h]

        Solving for k:

            k = house_demand / (LWT_curve(T_outdoor) − T_room)

        Purely thermal — no electrical energy or COP involved at this stage.
        We average over all non-trivial slots and fall back to a conservative
        estimate when the horizon is empty or all demands are near zero.
        """
        d = self.entry.data
        lwt_cold = float(d.get(CONF_LWT_HEATING_COLD, DEFAULT_LWT_HEATING_COLD))
        lwt_mild = float(d.get(CONF_LWT_HEATING_MILD, DEFAULT_LWT_HEATING_MILD))
        t_room = float(d.get(CONF_T_ROOM, DEFAULT_T_ROOM))

        k_samples: list[float] = []
        for pt in horizon:
            if pt.house_demand < 0.1:
                continue  # Near-zero demand (summer / warm day) — skip

            lwt_curve = _heating_curve_lwt(pt.t_outdoor, lwt_cold, lwt_mild)
            delta_t = lwt_curve - t_room
            if delta_t < 2.0:
                continue  # LWT barely above room temp — denominator too small

            k_samples.append(pt.house_demand / delta_t)

        if k_samples:
            k = sum(k_samples) / len(k_samples)
            _LOGGER.debug(
                "Emission coefficient k estimated from %d horizon slots: %.3f kW/K",
                len(k_samples),
                k,
            )
            return max(0.05, k)

        # Fallback: full pump output just meets design demand at cold-reference LWT.
        heat_pump_output = float(
            d.get(CONF_HEAT_PUMP_OUTPUT_KW, DEFAULT_HEAT_PUMP_OUTPUT_KW)
        )
        delta_t = max(1.0, lwt_cold - t_room)
        fallback_k = heat_pump_output / delta_t
        _LOGGER.debug(
            "Emission coefficient k: no usable horizon data, using fallback %.3f kW/K",
            fallback_k,
        )
        return fallback_k

    # ------------------------------------------------------------------
    # DHW ready-by index conversion
    # ------------------------------------------------------------------

    def _compute_ready_by_indices(
        self, horizon_start: datetime, n_hours: int
    ) -> list[int]:
        """Convert user-configured HH:MM ready-by strings to horizon slot indices.

        For each time string the method finds the first horizon slot whose
        **end** coincides with the specified hour.  Slot ``i`` covers the
        period ``[horizon_start + i·h,  horizon_start + (i+1)·h)`` and its
        ``tank_end`` value represents the DHW tank state at
        ``horizon_start + (i+1)·h``.  Checking slot ``i`` therefore enforces
        "tank ready at ``hh:00``" exactly.

        Times that fall before the horizon start (already past) are ignored;
        the solver will pick up tomorrow's occurrence naturally because the
        horizon typically spans 24 h.

        Example: ``horizon_start = 20:00``, ready-by ``"07:00"``
            → slot 10 ends at 07:00 the next morning → index 10.
        """
        times_str: str = self.entry.data.get(CONF_DHW_READY_TIMES, "")
        if not times_str or not times_str.strip():
            return []

        indices: list[int] = []
        for t_str in times_str.split(","):
            t_str = t_str.strip()
            if not t_str:
                continue
            try:
                hh, _mm = map(int, t_str.split(":"))
            except ValueError:
                _LOGGER.warning(
                    "DHW ready-by: cannot parse time %r — skipping.", t_str
                )
                continue
            for i in range(n_hours):
                slot_end = horizon_start + timedelta(hours=i + 1)
                if slot_end.hour == hh:
                    indices.append(i)
                    break
            else:
                _LOGGER.debug(
                    "DHW ready-by: time %r not found within %d-hour horizon — "
                    "constraint skipped for this solve.",
                    t_str,
                    n_hours,
                )

        result = sorted(set(indices))
        if result:
            _LOGGER.debug("DHW ready-by horizon indices: %s", result)
        return result

    # ------------------------------------------------------------------
    # MPC config builder
    # ------------------------------------------------------------------

    def _build_mpc_config(
        self, k_emission: float = 0.0, dhw_ready_by_hours: list[int] | None = None
    ) -> MpcConfig:
        """Construct an ``MpcConfig`` from the config entry."""
        d = self.entry.data
        dhw_enabled = bool(d.get(CONF_DHW_ENABLED, False))
        return MpcConfig(
            min_lwt=float(d.get(CONF_MIN_LWT, DEFAULT_MIN_LWT)),
            max_lwt=float(d.get(CONF_MAX_LWT, DEFAULT_MAX_LWT)),
            max_tank_temp=float(d.get(CONF_MAX_TANK_TEMP, DEFAULT_MAX_TANK_TEMP)),
            heat_pump_output_kw=float(
                d.get(CONF_HEAT_PUMP_OUTPUT_KW, DEFAULT_HEAT_PUMP_OUTPUT_KW)
            ),
            tank_volume_liters=float(d.get(CONF_TANK_VOLUME_L, DEFAULT_TANK_VOLUME_L)),
            lwt_step=float(d.get(CONF_LWT_STEP, DEFAULT_LWT_STEP)),
            tank_standby_loss_kwh=float(
                d.get(CONF_TANK_STANDBY_LOSS_KWH, DEFAULT_TANK_STANDBY_LOSS_KWH)
            ),
            min_output_kw=float(d.get(CONF_MIN_OUTPUT_KW, DEFAULT_MIN_OUTPUT_KW)),
            k_emission=k_emission,
            t_room=float(d.get(CONF_T_ROOM, DEFAULT_T_ROOM)),
            start_penalty_kwh=float(
                d.get(CONF_START_PENALTY_KWH, DEFAULT_START_PENALTY_KWH)
            ),
            dhw_enabled=dhw_enabled,
            dhw_tank_volume_liters=float(
                d.get(CONF_DHW_TANK_VOLUME_L, DEFAULT_DHW_TANK_VOLUME_L)
            ),
            dhw_min_temp=float(d.get(CONF_DHW_MIN_TEMP, DEFAULT_DHW_MIN_TEMP)),
            dhw_target_temp=float(d.get(CONF_DHW_TARGET_TEMP, DEFAULT_DHW_TARGET_TEMP)),
            dhw_lwt=float(d.get(CONF_DHW_LWT, DEFAULT_DHW_LWT)),
            dhw_ready_by_hours=dhw_ready_by_hours if dhw_ready_by_hours is not None else [],
        )


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions, no HA dependency)
# ---------------------------------------------------------------------------


def _heating_curve_lwt(
    t_outdoor: float,
    lwt_cold: float,
    lwt_mild: float,
) -> float:
    """
    Linearly interpolate the configured heating curve to get the target LWT
    at a given outdoor temperature.

    The curve is defined by two reference points:
    - ``(HEATING_CURVE_T_COLD, lwt_cold)`` — e.g. (−10 °C, 40 °C)
    - ``(HEATING_CURVE_T_MILD, lwt_mild)`` — e.g. (+10 °C, 28 °C)

    Result is clamped to ``[lwt_mild, lwt_cold]`` so extrapolation beyond
    the configured range never exceeds the user's intended LWT bounds.
    """
    span = HEATING_CURVE_T_MILD - HEATING_CURVE_T_COLD  # 20 K
    frac = (t_outdoor - HEATING_CURVE_T_COLD) / span
    lwt = lwt_cold + frac * (lwt_mild - lwt_cold)
    return max(lwt_mild, min(lwt_cold, lwt))


def _build_rh_map(weather_forecasts: list[dict]) -> dict[datetime, float]:
    """
    Build a ``{hour_dt: humidity%}`` lookup from HA weather forecast slots.

    Slots without a parseable datetime or humidity value are skipped silently.
    """
    rh_map: dict[datetime, float] = {}
    for fc in weather_forecasts:
        dt = _parse_slot_dt(fc.get("datetime", ""))
        if dt is None:
            continue
        humidity = fc.get("humidity")
        if humidity is not None:
            try:
                rh_map[dt] = float(humidity)
            except (TypeError, ValueError):
                pass
    return rh_map


def _parse_price_map(raw_prices: list[dict]) -> dict[datetime, float]:
    """
    Convert Nordpool ``raw_today``/``raw_tomorrow`` entries into a
    ``{hour_dt: price}`` lookup.

    Accepts both ``"start"`` and ``"datetime"`` as the timestamp key.
    """
    price_map: dict[datetime, float] = {}
    for slot in raw_prices:
        start_str = slot.get("start") or slot.get("datetime") or ""
        value = slot.get("value")
        if not start_str or value is None:
            continue
        dt = _parse_slot_dt(start_str)
        if dt is None:
            continue
        try:
            price_map[dt] = float(value)
        except (TypeError, ValueError):
            pass
    return price_map


def _parse_slot_dt(dt_str: str) -> datetime | None:
    """Parse a slot datetime string → timezone-aware local datetime (minute/second zeroed)."""
    if not dt_str:
        return None
    dt = dt_util.parse_datetime(dt_str)
    if dt is None:
        return None
    return dt_util.as_local(dt).replace(minute=0, second=0, microsecond=0)


def _lookup_price(price_map: dict[datetime, float], slot_dt: datetime) -> float | None:
    """Return the price for *slot_dt*, or ``None`` if absent."""
    return price_map.get(slot_dt)


def _result_to_dict(result: MpcResult) -> dict:
    """
    Flatten an ``MpcResult`` into the plain dict stored in ``coordinator.data``.

    Sensor entities should read individual keys via ``RESULT_*`` constants
    to stay decoupled from the solver's data structures.
    """
    next_run: str | None = None
    pump_on_now: bool = False

    if result.schedule:
        pump_on_now = result.schedule[0].pump_on
        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        for plan in result.schedule:
            if plan.pump_on:
                slot_dt = now + timedelta(hours=plan.hour_index)
                next_run = slot_dt.isoformat()
                break

    schedule = result.schedule

    # Aggregate horizon metrics — computed once here, read by sensor attributes.
    planned_run_hours = sum(1 for p in schedule if p.pump_on)
    planned_starts = sum(1 for p in schedule if p.start_event)
    # Thermal kWh delivered to the tank (pure thermal, no COP involved).
    planned_kwh_thermal = sum(p.heat_delivered_kwh for p in schedule)
    # Electrical kWh consumed (thermal delivered / COP per hour).
    # start_penalty is already in electricity_cost; back it out to report
    # actual compressor energy only (penalty is not "real" consumption).
    planned_kwh_electrical = sum(
        p.heat_delivered_kwh / p.cop_effective
        for p in schedule
        if p.pump_on and p.cop_effective > 0
    )

    return {
        RESULT_OPTIMAL_LWT: result.optimal_lwt,
        RESULT_OPTIMAL_OUTPUT_KW: result.optimal_output_kw,
        RESULT_TOTAL_COST: result.total_cost,
        RESULT_FEASIBLE: result.feasible,
        RESULT_SCHEDULE: schedule,
        RESULT_CURRENT_COP: (
            schedule[0].cop_effective if schedule else None
        ),
        RESULT_NEXT_RUN_START: next_run,
        RESULT_PUMP_ON_NOW: pump_on_now,
        RESULT_PLANNED_RUN_HOURS: planned_run_hours,
        RESULT_PLANNED_STARTS: planned_starts,
        RESULT_PLANNED_KWH_THERMAL: round(planned_kwh_thermal, 2),
        RESULT_PLANNED_KWH_ELECTRICAL: round(planned_kwh_electrical, 2),
        RESULT_DHW_ON_NOW: result.dhw_on_now,
        RESULT_DHW_SETPOINT: result.optimal_dhw_setpoint,
        RESULT_DHW_PLANNED_HOURS: result.dhw_planned_hours,
    }
