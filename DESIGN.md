# Heat Pump MPC — Design Document

## Purpose

A Home Assistant custom integration that consumes building heat demand from
Heating Analytics and produces a cost-optimal heat pump run schedule: which
hours to run, at what flow temperature (LWT), and at what inverter output
level. The integration does not model building physics — it consumes that
output and makes control decisions based on cost and efficiency.

## Scope

**In scope**
- COP estimation based on outdoor temperature and flow temperature (Carnot model)
- Defrost penalty estimation and runtime calibration
- Capacity derating at low outdoor temperatures (static prior + learned from Modbus)
- Inverter load modulation (min/full output as a second decision variable)
- Two-mass thermal buffer simulation (building demand + 300 L tank)
- Cost-optimal scheduling over a 12–48 h horizon
- Runtime η-learning from real measurements (EMA on Carnot efficiency)
- Capacity learning from implicit full load observations
- Persistent storage of all learned parameters across HA restarts
- Multi-instance support (one coordinator per config entry)

**Out of scope**
- Building physics modelling (owned by Heating Analytics)
- Weather forecasting (consumed from HA weather entity)
- Electricity price fetching (consumed from existing Nordpool/Tibber sensor)
- Modbus setpoint writing (handled by a separate HA automation)

---

## Data Flow

```
Heating Analytics          →  get_forecast service
                              → house_demand[t]  (kWh/h, per hour)
                              → t_outdoor[t]     (°C, inertia-adjusted)

HA Weather entity          →  get_forecasts (hourly)
                              → rh[t]            (%, for defrost penalty)

Price sensor (Nordpool)    →  raw_today / raw_tomorrow attributes
                              → price[t]         (currency/kWh, hourly)

Tank temperature sensor    →  tank_temp          (°C, current reading)

Learning sensors (optional, all from Modbus or heat meter):
  Electrical energy sensor →  cumulative kWh     (delta = consumption/interval)
  Thermal power sensor     →  instantaneous kW   (Track A)
  Flow + ΔT sensors        →  P = Q × ΔT × 1.163 (Track B, alt. to Track A)

─────────────────────────────────────────────────────────────────────────────
Heat Pump MPC (this integration)

  HeatPumpModel            →  COP(t_outdoor, rh, lwt, output_kw)
                           →  max_output(t_outdoor, lwt)
  MpcSolver                →  optimal schedule over horizon
  CopLearner               →  refines η_Carnot, f_defrost, capacity curve
  HeatpumpMpcStorage       →  persists learned state to .storage/

─────────────────────────────────────────────────────────────────────────────

Outputs (HA entities)
  number.lwt_setpoint               (°C, auto-tracks solver recommendation)
  sensor.optimal_flow_temp          (°C)
  sensor.optimal_output             (kW)
  sensor.estimated_cop              (dimensionless)
  sensor.projected_heating_cost     (currency)
  sensor.next_run_start             (timestamp)
  binary_sensor.pump_on_now         (boolean)
  binary_sensor.schedule_feasible   (problem device class, True = infeasible)
  binary_sensor.dhw_on_now          (boolean)
  number.dhw_setpoint               (°C)
```

---

## COP Model (`core/heat_pump_model.py`)

### Base COP — Carnot lift interpolation

COP is fundamentally a function of *lift*:

```
lift(t, LWT) = LWT − T_outdoor[t]
COP_base = f(lift)   ← interpolated from EN 14511 datasheet points
```

Default curve (Sprsun R290, actual datasheet values):

| Condition | Lift (K) | COP (full load) |
|-----------|----------|-----------------|
| A7/W35    | 28       | 4.42            |
| A7/W45    | 38       | 3.64            |
| A-7/W35   | 42       | 3.33 (extrap.)  |

Points are linearly interpolated; extrapolation continues the nearest slope,
clamped to COP ≥ 1.0.

### Defrost penalty

```
COP_effective = COP_base × f_defrost
f_defrost = 0.85  when T_outdoor < 7°C AND RH > 70%
          = 1.0   otherwise
```

Both the threshold values and the penalty factor are calibrated at runtime
by `CopLearner`.

### Load modulation — COP at partial output

Inverter heat pumps achieve higher COP at part-load. The modulation gain
(min-load COP / full-load COP) is interpolated from:

| Condition | Lift (K) | Gain  |
|-----------|----------|-------|
| A7/W35    | 28       | 1.269 |
| A7/W45    | 38       | 1.173 |

`get_cop_at_output(t_outdoor, rh, lwt, output_kw, max_output_kw, min_output_kw)`
linearly interpolates between min-load and full-load COP based on requested
output, then applies the defrost penalty.

### Capacity derating — `get_max_output_at(t_outdoor, lwt, rated_kw)`

Inverter heat pumps lose the ability to deliver their rated output at low
outdoor temperatures and/or high LWT. The MPC caps heat delivery per hour
to prevent scheduling hours the pump cannot physically fulfil.

Capacity is modelled as a separable product of two factors:

```
capacity = rated_kW × f_temp(T_outdoor) × f_lwt(LWT)
```

Default priors (Sprsun R290 estimates; replaced by learned values once
enough compressor frequency data has accumulated):

| T_outdoor | f_temp | Source     |
|-----------|--------|------------|
| 7°C       | 1.00   | Rating point |
| −7°C      | 0.78   | Estimated  |
| −15°C     | 0.63   | Estimated  |
| −20°C     | 0.52   | Estimated  |

| LWT  | f_lwt | Source     |
|------|-------|------------|
| 35°C | 1.00  | Reference  |
| 45°C | 0.95  | Estimated  |
| 55°C | 0.89  | Estimated  |

Both curves are interpolated with `_interp_1d`; learned anchors at −7°C
and −15°C replace the static estimates once reliable (≥ 5 samples each).

---

## MPC Solver (`core/mpc_solver.py`)

### Buffer Models

**Mass 1 — Building demand**
Consumed from Heating Analytics `get_forecast`. The building requires
`house_demand[t]` kWh in hour `t` to maintain the comfort band. This
integration does not model the building; it treats demand as a given input.

**Mass 2 — Space Heating Buffer Tank**
The tank acts as a fast thermal battery between the heat pump and the
building's shunt circuit.

- Capacity: 300 L × 1.16 Wh/(L·K) = 0.348 kWh/K
- State equation:
  ```
  tank_energy[t+1] = tank_energy[t]
                   + heat_delivered[t]
                   − house_demand[t]
                   − tank_standby_loss_kwh
  ```
- `heat_delivered[t]` = `min(output_kw[t], max_capacity_at(t), headroom_to_max_temp)`
- **Comfort constraint**: `tank_energy[t] ≥ 0` in every hour (tank never depleted)
- **Safety constraint**: tank temperature ≤ `max_tank_temp`
- **Charging ceiling constraint**: tank temperature ≤ `LWT`. The pump cannot charge a tank that is already at or above the current flow temperature, as heat only flows from hot to cold. This prevents the solver from accumulating a high buffer temperature in the tank while running at an artificially low and efficient LWT.

**Mass 3 — DHW (Domestic Hot Water) Tank (Optional)**
If `dhw_enabled` is True, an additional DHW tank model is considered.

- Capacity: 180 L × 1.16 Wh/(L·K) (configurable)
- A daily demand `dhw_daily_demand_kwh` is divided by 24 and drawn each hour.
- **Constraints:** The tank must never be depleted below `dhw_min_temp`.
- **Ready-by Constraint:** Optional user-defined hours where the tank must be at ≥ 90% capacity.
- **Mutual Exclusivity:** DHW and SH modes are mutually exclusive. DHW is scheduled first, greedily claiming the cheapest hours to satisfy its constraints. Hours assigned to DHW are blocked from SH scheduling.

### Decision variables

The outer loop is over **LWT** — discrete candidates from `min_lwt` to
`max_lwt` in `lwt_step` increments. These candidates are further filtered by an **emission constraint**:
`LWT_min = t_room + peak_demand / k_emission`
This ensures the solver chooses an LWT physically high enough to satisfy the building's thermal demand via the emission system, preventing it from "cheating" by picking a lower, more efficient flow temperature that cannot deliver the required heat.

For each valid LWT candidate the solver
performs **per-hour inverter output selection** (see algorithm below)
rather than evaluating the whole horizon at a fixed output level.

The cheapest feasible LWT wins; if none are feasible, the least-violated
scenario is returned with `feasible=False`.

### Decision metric

```
cost_per_kwh_heat(t, LWT, output_kw) = price[t] / COP_effective(t, LWT, output_kw)
```

With a uniform price (no price sensor configured) this equals `1 / COP`,
so the solver minimises total electricity consumption, concentrating
runtime in the highest-COP windows.

### Per-hour output selection algorithm

For each LWT candidate the algorithm proceeds in three phases.

**Phase 1 — Constraint satisfaction at full output**

Repeat until `tank_energy[t] ≥ 0` in every hour:
1. Find the earliest hour where `tank_energy[t] < 0`.
2. Schedule the cheapest unscheduled hour at or before that point,
   evaluated at full rated output (`heat_pump_output_kw`).
3. Re-simulate.

Using full output here fills the tank as quickly as possible, which
minimises the number of forced pump-on hours needed to satisfy hard
constraints. Causal constraint respected: cannot pre-heat retroactively.

**Post-Phase-1 downgrade — switch to min output where feasible**

For each pump-on hour added in Phase 1:
- Tentatively switch its output to `min_output_kw`.
- Re-simulate; if still fully feasible keep the downgrade.
- Otherwise revert to full output.

`min_output_kw` delivers heat at a better COP (modulation gain), so
the same thermal energy costs less electricity. The downgrade is kept
whenever the lower heat rate per hour does not cause a constraint
violation elsewhere in the horizon.

**Phase 2 — Opportunistic pre-charging at min output**

Consider the remaining off-hours in ascending `cost_per_kwh_heat` order
(highest COP first):
- Tentatively add the hour at `min_output_kw`.
- Re-simulate; keep only if `heat_delivered[t] > 0` — i.e. the tank had
  actual headroom at that point in the forward simulation.

Because Phase 1 used full output and typically fills the tank to near
capacity quickly, most candidate hours in Phase 2 find `heat_delivered = 0`
and are rejected. This produces the natural on/off pattern that correctly
concentrates runtime in high-COP windows while the stored tank energy
covers demand during lower-efficiency periods.

**Why this avoids "always-on at min load"**

If the solver only evaluated constant-output scenarios, the min-output
scenario would always have the best aggregate COP and would "win" — but
Phase 2 would then find every off-hour has a tiny bit of headroom (the
tank drains slowly) and add them all. With per-hour output selection,
Phase 1 charges the tank quickly at full output, leaving Phase 2 with a
full tank and very little headroom, so most hours are correctly rejected.

### Output

`MpcResult` contains:
- `optimal_lwt` — LWT setpoint for the chosen scenario
- `optimal_output_kw` — inverter output level chosen for **hour 0** (the
  current action); subsequent hours may differ — see `schedule[t].output_kw`
- `feasible` — whether all tank constraints were met
- `total_cost` — projected electricity cost over the horizon
- `schedule` — per-hour `HourPlan` list with `output_kw`, `max_capacity_kw`,
  `cop_effective`, `heat_delivered_kwh`, `tank_energy_kwh`, `electricity_cost`

---

## Runtime Learning (`core/cop_learner.py`)

### What is learned and why

| Parameter | What it is | Why learn it |
|-----------|-----------|--------------|
| η_Carnot | Real COP / Carnot COP | Single number that corrects the full COP curve for this specific unit |
| f_defrost | COP penalty during icing | Varies by unit condition; 0.85 is a starting estimate |
| Capacity fractions | Fraction of rated output at −7°C and −15°C | Static estimates are uncertain; Modbus reveals the truth |

### COP learning — tracks A and B

Every completed run cycle produces one `CopObservation`:
- `heat_out_kwh` — from thermal power sensor (Track A) or flow × ΔT (Track B)
- `elec_kwh` — delta of cumulative electrical energy sensor
- `t_outdoor`, `rh`, `lwt` — from sensors at time of observation

From these:
```
COP_measured = heat_out / elec
η_measured   = COP_measured / COP_Carnot(T_outdoor, LWT)
```

Routing:
- **No suspected icing** (clean conditions): EMA-update η_Carnot (α = 0.04)
- **Suspected icing** (T < 7°C, RH > 70%): EMA-update f_defrost as
  `η_measured / η_Carnot_current` (α = 0.07)

Quality guards reject observations with: COP outside [1.0, 7.0], lift < 5 K,
duration < 15 min, heat < 0.05 kWh, or electrical energy ≤ 0.

The η_Carnot estimate is considered reliable after 20 clean-condition samples.

### Capacity learning — implicit full load

**COP Contamination Filter**: When learning COP, it is crucial to filter out observations from DHW cycles or defrosts, as they distort the SH COP curve. `CONF_DHW_OPERATION_SENSOR` provides a direct signal indicating when the HP is actively heating the DHW tank. If not available, a temperature-rise heuristic on `CONF_DHW_TEMP_SENSOR` serves as a fallback.

**The observability problem**: observed thermal output is ambiguous — we
cannot tell whether the pump was running at maximum or modulated down.

**Solution**: The MPC knows when it commands full output (`is_full_load`).
If the pump is commanded to run at full load, and the tank has enough headroom (`tank_headroom_kwh`) to absorb more heat than was delivered, then the tank was not the limiting factor. In this case, `heat_out_kW / rated_kW` is a direct measurement of the capacity fraction at the current outdoor temperature. This implicitly replaces the need for a compressor frequency sensor.

**Temperature anchors**: Observations are binned to the nearest anchor
(−15°C or −7°C, within ±6°C). Each anchor uses EMA (α = 0.03 — very slow,
since physical capacity is stable). 5 reliable observations per anchor
before the learned value replaces the static prior in `HeatPumpModel`.

Cold-start values (Sprsun R290 datasheet defaults) are used until
sufficient data is available. The transition from prior to learned is
selective: each anchor transitions independently.

### Persistence (`storage.py`)

`HeatpumpMpcStorage` wraps `homeassistant.helpers.storage.Store`.
Stored as `.storage/heatpump_mpc.<entry_id>` (schema v1):

```json
{
  "version": 1,
  "learner": {
    "eta_carnot": 0.421,
    "f_defrost": 0.847,
    "capacity_frac_minus15": 0.651,
    "capacity_frac_minus7": 0.763,
    "capacity_minus15_samples": 12,
    "capacity_minus7_samples": 31,
    "eta_carnot_samples": 203,
    "f_defrost_samples": 61
  }
}
```

Writes are debounced (30 s) so frequent learning updates coalesce into
one disk write. On HA restart, `coordinator.async_setup()` loads state
before the first solver run.

---

## Coordinator (`coordinator.py`)

Runs every 30 minutes as a `DataUpdateCoordinator`.

**Each update cycle:**
1. Fetch HA forecast (`heating_analytics.get_forecast`)
2. Fetch weather forecast for RH (`weather.get_forecasts`)
3. Read Nordpool price sensor attributes
4. Read tank temperature sensor
5. Build `HorizonPoint` list (merge demand, RH, price per hour)
6. Run learning pipeline (`_learn_from_sensors`):
   - Compute electrical energy delta since last update
   - Read thermal power (Track A or B)
   - Submit `CopObservation` to `CopLearner`
   - Save learner state to storage (debounced)
7. Apply learned capacity anchors to `HeatPumpModel`
8. Run `MpcSolver.solve()`
9. Publish result dict to sensor entities

---

## Configuration

### Step 1 — Data sources
| Parameter | Description |
|-----------|-------------|
| Price sensor | Nordpool/Tibber entity with `raw_today`/`raw_tomorrow` attributes |
| Tank temperature sensor | Buffer tank temperature (°C) |
| Weather entity | HA weather integration (for hourly RH) |
| HA entity ID *(optional)* | Routes `get_forecast` to the correct Heating Analytics instance |

### Step 2 — Heat pump & tank
| Parameter | Default | Description |
|-----------|---------|-------------|
| Nominal output (full load) | 5.0 kW | Rated heating capacity |
| Minimum inverter output | 4.37 kW | Minimum output at lowest inverter speed |
| Min flow temperature | 35°C | Absolute floor, never violated |
| Max flow temperature | 55°C | Highest LWT the scheduler may target |
| Tank safety ceiling | 55°C | Charging stops at this temperature |
| Buffer tank volume | 300 L | Used to compute thermal capacity |

### Step 3 — COP learning
| Parameter | Description |
|-----------|-------------|
| Electrical energy sensor | Cumulative kWh on heat pump supply |
| Thermal power sensor *(Track A)* | Instantaneous kW from heat meter |
| Use flow sensors *(Track B toggle)* | Derive power from flow × ΔT instead |
| Flow rate sensor | L/min or m³/h |
| Supply temp sensor | Water temp leaving the heat pump |
| Return temp sensor | Water temp returning to the heat pump |

### Step 4 — Schedule tuning
| Parameter | Default | Description |
|-----------|---------|-------------|
| Horizon | 24 h | How far ahead to optimise |
| LWT step | 5°C | Granularity of flow temperature candidates |
| Tank standby loss | 0.05 kWh/h | Insulation loss from the buffer tank |

### Step 5 — DHW Scheduling (Optional)
| Parameter | Default | Description |
|-----------|---------|-------------|
| DHW enabled | False | Toggle for DHW features |
| DHW temp sensor | — | Current DHW tank temperature |
| DHW operation sensor | — | Binary sensor indicating active DHW mode (for COP filtering) |
| DHW tank volume | 180 L | Used to compute thermal capacity |
| DHW min temp | 40°C | DHW tank reheating threshold |
| DHW target temp | 55°C | Temperature HP heats DHW tank to |
| DHW LWT | 55°C | Fixed LWT used during DHW mode |
| DHW daily demand | 3.5 kWh | Total daily thermal demand |
| DHW ready times | — | Hours where DHW must be at 90% capacity (e.g. `07:00`) |

---

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `number.lwt_setpoint` | °C | LWT setpoint that tracks the solver recommendation; writable for manual override (reset on next coordinator update) |
| `number.dhw_setpoint` | °C | Recommended DHW target temperature |
| `sensor.optimal_flow_temp` | °C | Read-only mirror of the solver's recommended LWT; `schedule` attribute has full per-hour plan |
| `sensor.optimal_output` | kW | Inverter output level for the current hour (hour 0 of the optimal schedule) |
| `sensor.estimated_cop` | — | Effective COP for the current hour |
| `sensor.projected_heating_cost` | currency | Total cost over horizon |
| `sensor.next_run_start` | timestamp | Next scheduled pump-on hour |
| `binary_sensor.pump_on_now` | boolean | Whether the current hour is scheduled for Space Heating |
| `binary_sensor.dhw_on_now` | boolean | Whether the current hour is scheduled for DHW |
| `binary_sensor.schedule_feasible` | problem | `on` = infeasible (tank cannot meet demand) |

Modbus setpoint writing is handled by a separate HA automation that reads
`number.lwt_setpoint` (which auto-tracks the MPC recommendation) and writes
the value to the heat pump. The user controls the automation logic.

---

## File Structure

```
custom_components/heatpump_mpc/
├── __init__.py          # async_setup_entry / async_unload_entry
├── coordinator.py       # DataUpdateCoordinator, learning pipeline
├── sensor.py            # 5 sensor entities
├── binary_sensor.py     # 2 binary sensor entities
├── number.py            # 1 number entity (LWT setpoint, writable)
├── config_flow.py       # 4-step setup + reconfigure flow
├── storage.py           # Persistent learner state (HA Store wrapper)
├── const.py             # All config keys, defaults, result keys
├── strings.json         # UI strings for config flow
├── manifest.json
└── core/
    ├── heat_pump_model.py   # COP curve, modulation gain, capacity derating
    ├── mpc_solver.py        # Greedy two-phase MPC, tank simulation
    └── cop_learner.py       # η_Carnot + f_defrost + capacity EMA learning
```

---

## Open Items

- **Actual Sprsun R290 capacity table** — full heating output vs outdoor
  temperature from the datasheet (not just COP data). The current capacity
  prior at −7°C and −15°C are estimates; implicit capacity learning will
  replace them over time.
- **Price sensor** — currently optional. When absent, uniform price = 1.0
  is used so the solver optimises purely for efficiency (lowest kWh consumed).
  With a Nordpool/Tibber sensor connected, the solver additionally shifts
  load to cheap hours.
