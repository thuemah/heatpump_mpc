"""Constants for the Heat Pump MPC integration."""

DOMAIN = "heatpump_mpc"

# ---------------------------------------------------------------------------
# Operation mode
# ---------------------------------------------------------------------------

CONF_OPERATION_MODE = "operation_mode"
"""``"full_mpc"`` runs the scheduler + COP learning.
``"cop_only"`` disables the scheduler and exposes COP learning + reporting
only (no tank, LWT, DHW, or schedule configuration needed)."""

OP_MODE_FULL_MPC = "full_mpc"
OP_MODE_COP_ONLY = "cop_only"

# ---------------------------------------------------------------------------
# Heat pump type and refrigerant
# ---------------------------------------------------------------------------

CONF_HP_TYPE = "hp_type"
"""Heat pump type: ``"ashp"`` (air-to-water), ``"gshp"`` (water-to-water /
ground source), ``"a2a"`` (air-to-air, COP-only)."""

HP_TYPE_ASHP = "ashp"
HP_TYPE_GSHP = "gshp"
HP_TYPE_A2A = "a2a"

CONF_REFRIGERANT = "refrigerant"
"""Refrigerant type: ``"r290"``, ``"r32"``, ``"r410a"``, ``"other"``."""

CONF_BRINE_TEMP_SENSOR = "brine_temp_sensor"
"""Sensor reporting brine inlet temperature (°C) for GSHP installations.
Used as the source temperature for COP calculation instead of outdoor temp."""

# ---------------------------------------------------------------------------
# Config entry keys (what the user configures)
# ---------------------------------------------------------------------------

CONF_WEATHER_ENTITY = "weather_entity"
"""HA weather entity used for the hourly T_outdoor / RH forecast."""

CONF_PRICE_SENSOR = "price_sensor"
"""Nordpool or Tibber sensor entity that carries hourly electricity prices."""

CONF_HA_ENTITY_ID = "ha_entity_id"
"""Entity ID of any Heating Analytics sensor belonging to the target instance.
Used to route the heating_analytics.get_forecast service call to the correct
coordinator when multiple HA instances are installed."""

CONF_TANK_TEMP_SENSOR = "tank_temp_sensor"
"""Sensor reporting current buffer tank temperature (°C)."""

CONF_MIN_LWT = "min_lwt"
"""Minimum leaving water temperature the heat pump may target (°C)."""

CONF_MAX_LWT = "max_lwt"
"""Maximum leaving water temperature the heat pump may target (°C)."""

CONF_MAX_TANK_TEMP = "max_tank_temp"
"""Safety ceiling for the buffer tank (°C)."""

CONF_HEAT_PUMP_OUTPUT_KW = "heat_pump_output_kw"
"""Nominal thermal output of the heat pump (kW = kWh per 1-hour step)."""

CONF_MIN_OUTPUT_KW = "min_output_kw"
"""Minimum inverter thermal output (kW). The solver tests this against the full
rated output and picks whichever is cheaper. Default: 4.37 kW (Sprsun R290)."""

CONF_RATED_MAX_ELEC_KW = "rated_max_elec_kw"
"""Rated maximum electrical input power of the heat pump (kW).
Found in the datasheet.  Used as the reference for the measured-electrical-load
gate: capacity learning only triggers when the actual electrical draw exceeds
90 % of this value, proving the compressor was running at full capacity."""

CONF_TANK_VOLUME_L = "tank_volume_liters"
"""Buffer tank volume (litres). Default: 300 L."""

CONF_LWT_STEP = "lwt_step"
"""Increment between consecutive LWT candidates evaluated by the solver (°C)."""

CONF_TANK_STANDBY_LOSS_KWH = "tank_standby_loss_kwh"
"""Estimated hourly heat loss from the insulated tank (kWh)."""

CONF_HORIZON_HOURS = "horizon_hours"
"""Number of hours to optimise over (12–48). Default: 24."""

CONF_START_PENALTY_KWH = "start_penalty_kwh"
"""Electrical energy equivalent wasted during each compressor start (kWh_el).
Added to the scenario cost for every pump start event so the solver prefers
fewer, longer run periods over fragmented short cycles."""

CONF_LWT_HEATING_COLD = "lwt_heating_cold"
"""Target LWT at the design cold outdoor temperature (heating curve cold setpoint, °C)."""

CONF_LWT_HEATING_MILD = "lwt_heating_mild"
"""Target LWT at the mild outdoor reference temperature (heating curve mild setpoint, °C)."""

CONF_T_ROOM = "t_room"
"""Indoor comfort temperature (°C). Used to compute the minimum LWT the emission
system needs to transfer the required thermal power to the building."""

# COP learning — measurement sensors
CONF_ELECTRICAL_ENERGY_SENSOR = "electrical_energy_sensor"
"""Cumulative energy sensor (kWh) on the heat pump's electrical supply.
Used to calculate actual COP = heat_out / power_in."""

CONF_THERMAL_POWER_SENSOR = "thermal_power_sensor"
"""Instantaneous thermal power sensor (kW) — e.g. from a heat meter.
Primary source for heat_out when available."""

# COP learning — flow/temperature sensors (Track B, optional)
CONF_USE_FLOW_SENSORS = "use_flow_sensors"
"""When True, thermal power is derived from flow rate × ΔT instead of
a dedicated power sensor."""

CONF_FLOW_RATE_SENSOR = "flow_rate_sensor"
"""Water flow rate sensor.  Must be in L/min or m³/h (configured via
CONF_FLOW_UNIT)."""

CONF_SUPPLY_TEMP_SENSOR = "supply_temp_sensor"
"""Flow / supply temperature sensor (°C) — water leaving the heat pump."""

CONF_RETURN_TEMP_SENSOR = "return_temp_sensor"
"""Return temperature sensor (°C) — water entering the heat pump."""

CONF_FLOW_UNIT = "flow_unit"
"""Unit of the flow rate sensor: 'L/min' or 'm³/h'."""

FLOW_UNIT_LMIN = "L/min"
FLOW_UNIT_M3H = "m³/h"

# DHW (Domestic Hot Water) — all optional; feature disabled when dhw_enabled is False.
CONF_DHW_ENABLED = "dhw_enabled"
"""True when the DHW scheduling and COP-filter features are active."""

CONF_DHW_MODE = "dhw_mode"
"""Selects the DHW topology: ``"separate_tank"`` (classic: dedicated DHW
tank with mode-switching) or ``"coil_in_tank"`` (spiral in the SH buffer
tank — no mode-switching, spiral demand is an additional SH-tank load)."""

DHW_MODE_SEPARATE = "separate_tank"
DHW_MODE_COIL = "coil_in_tank"

CONF_DHW_TEMP_SENSOR = "dhw_temp_sensor"
"""Sensor reporting current DHW tank temperature (°C).
Used for the COP-contamination filter and to initialise the DHW tank model."""

CONF_DHW_OPERATION_SENSOR = "dhw_operation_sensor"
"""Binary sensor that reads ``on`` when the heat pump is actively heating the
DHW tank.  When configured, used as the primary signal for the COP
contamination filter — more reliable than the temperature-rise fallback because
it detects DHW mode even when the tank is already near target temperature."""

CONF_DHW_TANK_VOLUME_L = "dhw_tank_volume_liters"
"""DHW tank volume (litres). Default: 180 L."""

CONF_DHW_MIN_TEMP = "dhw_min_temp"
"""Temperature below which the DHW tank is considered depleted and reheating
is required (°C). Default: 40 °C."""

CONF_DHW_TARGET_TEMP = "dhw_target_temp"
"""Target temperature the HP heats the DHW tank to (°C). Default: 55 °C."""

CONF_DHW_LWT = "dhw_lwt"
"""Fixed leaving water temperature used during DHW mode (°C). Must be ≥
dhw_target_temp. Default: 55 °C."""

CONF_DHW_DAILY_DEMAND_KWH = "dhw_daily_demand_kwh"
"""Estimated total thermal energy drawn from the DHW tank per day (kWh_th).
Distributed uniformly over 24 h for horizon planning. Default: 3.5 kWh."""

CONF_DHW_READY_TIMES = "dhw_ready_times"
"""Comma-separated HH:MM strings specifying when the DHW tank must be fully
heated (e.g. ``07:00, 18:00``).  At each listed time the scheduler enforces a
hard constraint that the tank is at ≥ 90 % of its target energy.
Leave blank to disable (only the never-empty constraint applies)."""

# Coil-in-tank specific
CONF_COIL_ENERGY_SENSOR = "coil_energy_sensor"
"""Cumulative energy sensor (kWh) on the DHW spiral circuit.
Used to correct Track C (subtract spiral load from SH accumulation)."""

CONF_COIL_DAILY_DEMAND_KWH = "coil_daily_demand_kwh"
"""Estimated daily thermal energy drawn through the DHW spiral (kWh_th/day).
Distributed uniformly over 24 h as additional SH-tank demand."""

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_WEATHER_ENTITY = "weather.forecast_home"
DEFAULT_PRICE_SENSOR = "sensor.nordpool_kwh_no2_nok_3_10_025"
DEFAULT_TANK_TEMP_SENSOR = "sensor.buffer_tank_temperature"

DEFAULT_MIN_LWT = 35.0          # °C
DEFAULT_MAX_LWT = 55.0          # °C
DEFAULT_MAX_TANK_TEMP = 55.0    # °C
DEFAULT_HEAT_PUMP_OUTPUT_KW = 5.0
DEFAULT_MIN_OUTPUT_KW = 4.37    # kW  — Sprsun R290 min inverter output
DEFAULT_RATED_MAX_ELEC_KW = 3.5 # kW  — typical R290 max electrical input
DEFAULT_TANK_VOLUME_L = 300.0   # litres
DEFAULT_LWT_STEP = 5.0          # °C
DEFAULT_TANK_STANDBY_LOSS_KWH = 0.05   # kWh / hour
DEFAULT_HORIZON_HOURS = 24
DEFAULT_START_PENALTY_KWH = 0.2        # kWh_el — ~10 min inefficient startup at ~1.2 kW_el

DEFAULT_TANK_TEMP = 40.0        # °C  — fallback when sensor is unavailable
DEFAULT_RH = 75.0               # %   — fallback when weather entity has no humidity
DEFAULT_FLOW_UNIT = FLOW_UNIT_LMIN
DEFAULT_UNIFORM_PRICE = 1.0     # dimensionless — used when no price sensor is configured

# DHW defaults
DEFAULT_DHW_TANK_VOLUME_L = 180.0     # litres
DEFAULT_DHW_MIN_TEMP = 40.0           # °C — below this the DHW tank needs reheating
DEFAULT_DHW_TARGET_TEMP = 55.0        # °C — HP heats DHW tank to this temperature
DEFAULT_DHW_LWT = 55.0                # °C — fixed LWT during DHW mode
DEFAULT_DHW_DAILY_DEMAND_KWH = 3.5    # kWh_th per day
DEFAULT_DHW_TANK_TEMP = 50.0          # °C — fallback when DHW sensor is unavailable
DEFAULT_DHW_READY_TIMES = ""          # empty = no ready-by constraints
DEFAULT_DHW_MODE = DHW_MODE_SEPARATE  # classic separate-tank mode
DEFAULT_COIL_DAILY_DEMAND_KWH = 5.0   # kWh_th per day — typical spiral load

# Heating curve reference outdoor temperatures (fixed, not user-configurable)
HEATING_CURVE_T_COLD: float = -10.0   # °C — design cold point
HEATING_CURVE_T_MILD: float = 10.0    # °C — mild reference point

DEFAULT_LWT_HEATING_COLD: float = 40.0  # °C — floor heating default at -10°C
DEFAULT_LWT_HEATING_MILD: float = 28.0  # °C — floor heating default at +10°C
DEFAULT_T_ROOM: float = 21.0            # °C — indoor comfort setpoint

# Heating Analytics domain (for service calls)
HA_DOMAIN = "heating_analytics"
HA_SERVICE_GET_FORECAST = "get_forecast"

# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

UPDATE_INTERVAL_MINUTES = 30

# ---------------------------------------------------------------------------
# Keys used in coordinator.data (available to sensor entities)
# ---------------------------------------------------------------------------

RESULT_OPTIMAL_LWT = "optimal_lwt"
"""Optimal leaving water temperature for the current solve (°C)."""

RESULT_OPTIMAL_OUTPUT_KW = "optimal_output_kw"
"""Optimal inverter output level for the current solve (kW)."""

RESULT_TOTAL_COST = "total_cost"
"""Projected total electricity cost over the horizon (currency)."""

RESULT_FEASIBLE = "feasible"
"""True when the solver found a schedule that satisfies all constraints."""

RESULT_SCHEDULE = "schedule"
"""Full list[HourPlan] for the solved horizon."""

RESULT_CURRENT_COP = "current_cop"
"""Effective COP estimated for the current hour at the optimal LWT."""

RESULT_NEXT_RUN_START = "next_run_start"
"""ISO timestamp of the next scheduled pump start (or None)."""

RESULT_PUMP_ON_NOW = "pump_on_now"
"""True when the heat pump should be running in the current hour."""

RESULT_PLANNED_RUN_HOURS = "planned_run_hours"
"""Number of hours the pump is scheduled ON in the current horizon."""

RESULT_PLANNED_STARTS = "planned_starts"
"""Number of compressor start events in the current horizon."""

RESULT_PLANNED_KWH_THERMAL = "planned_kwh_thermal"
"""Total thermal energy scheduled to be delivered to the tank (kWh_th)."""

RESULT_PLANNED_KWH_ELECTRICAL = "planned_kwh_electrical"
"""Estimated total electrical consumption over the horizon (kWh_el),
excluding start penalties."""

RESULT_DHW_ON_NOW = "dhw_on_now"
"""True when the DHW tank should be reheated in the current hour."""

RESULT_DHW_SETPOINT = "dhw_setpoint"
"""Recommended DHW tank target temperature (°C) to write to the heat pump.
Equal to dhw_target_temp when DHW is scheduled; dhw_min_temp - 1 otherwise."""

RESULT_DHW_PLANNED_HOURS = "dhw_planned_hours"
"""Number of DHW-mode hours scheduled in the current horizon."""

RESULT_SH_THERMAL_ENERGY_TOTAL_KWH = "sh_thermal_energy_total_kwh"
"""Cumulative space-heating thermal energy delivered since integration startup (kWh_th).
Monotonically increasing; exposed as a ``total_increasing`` energy sensor so
Heating Analytics can derive an hourly delta by comparing successive values."""

# ---------------------------------------------------------------------------
# Service names
# ---------------------------------------------------------------------------

SERVICE_GET_SH_HOURLY = "get_sh_hourly"
"""Service that returns a rolling buffer of completed per-hour SH thermal energy
records.  Heating Analytics calls this to retrieve actual COP/energy data."""

SERVICE_GET_COP_PARAMS = "get_cop_params"
"""Service that returns the current COP model parameters (η_Carnot, f_defrost,
thresholds, and the current LWT setpoint).  Heating Analytics uses these to
compute per-hour COP in the Track C midnight sync, replacing the less accurate
daily-average COP."""
