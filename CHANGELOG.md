# Changelog

## v0.2.0 — 2026-04-04

### New Features

- **Multi-type heat pump support:** Air-to-water (ASHP), water-to-water
  (GSHP), and air-to-air (A2A) heat pump types with tailored cold-start
  COP priors, capacity derating curves, and defrost behaviour.  Selectable
  refrigerant profiles (R290, R32, R410A, other) speed up initial
  convergence — CopLearner refines from real data regardless.

- **COP-only operation mode:** Lightweight mode that disables the MPC
  scheduler entirely — only COP learning, SH energy tracking, and the
  `get_cop_params`/`get_sh_hourly` services are active.  Ideal for
  getting started, air-to-air units without a buffer tank, or installations
  where you want COP reporting before committing to full scheduling.
  Upgrade to full MPC at any time via reconfigure.

- **GSHP brine temperature support:** For ground-source installations, a
  brine inlet sensor replaces outdoor air temperature in all COP and
  capacity calculations.  Defrost tracking is automatically disabled
  (no evaporator icing in brine circuits).

- **Coil-in-tank DHW:** New DHW topology for systems where a spiral heat
  exchanger sits inside the SH buffer tank.  The heat pump always runs in
  SH mode at optimal COP — no mode-switching, no high-LWT DHW cycles.
  Spiral demand is subtracted from the SH tank simulation; ready-by
  constraints enforce a minimum tank energy reserve.  Optional spiral
  energy sensor corrects Track C data.

- **`get_cop_params` service:** Returns learned COP model parameters
  (η_Carnot, f_defrost, thresholds, LWT) so Heating Analytics can compute
  per-hour COP in the Track C midnight sync — replacing the less accurate
  daily-average COP.

- **Multi-unit forecast isolation:** MPC now sends `isolate_sensor` in the
  `get_forecast` call, receiving `max(0, global − Σ other_units)` — only
  its share of the building's demand.  Prevents double-accounting when
  panel heaters or secondary heat pumps are present.

### Solver Improvements

- **SH/DHW conflict resolution:** When DHW blocks hours that SH needs for
  survival, the solver retries all LWT candidates without DHW constraints.
  Space heating always takes priority — the house will not freeze.

- **Capacity-aware COP estimation:** The solver passes the physical capacity
  ceiling (derated by outdoor temperature) to the COP model.  At extreme
  cold where capacity drops below `min_output_kw`, the model correctly
  returns full-load COP instead of the (better) min-load value.

- **Cost-aware DHW Phase 1:** DHW survival scheduling picks the cheapest
  eligible hour (price / COP) instead of the latest, reducing electricity
  cost while still satisfying never-empty and ready-by constraints.

- **Track C standby loss correction:** Buffer-tank standby losses are
  subtracted from the SH hourly buffer before it reaches Heating Analytics,
  preventing the building's learned U-value from being inflated by tank
  insulation losses.

### Robustness

- **Sensor source-data protection:** Comprehensive input validation inspired
  by Heating Analytics' proven spike/reset guards:
  - Electrical energy spike filter (rejects deltas > rated × duration × 1.5)
  - Thermal power plausibility (rejects > 2× rated thermal output)
  - Tank temperature range checks ([0, 95] °C for both SH and DHW)
  - Flow sensor bounds (negative flow, ΔT > 25 K, computed power cap)
  - SH accumulation plausibility cap (last-resort Track C guard)

- **Stale sensor watchdog:** Buffer tank, DHW tank, and thermal power
  sensors are rejected when `last_updated` exceeds 60 minutes.

- **Per-cycle SH accumulator persistence:** In-progress hour state survives
  HA restarts, eliminating systematic under-reporting of Track C data.

### Breaking Changes

- `max_tank_temp ≥ max_lwt` validation removed — these are now independent
  parameters.  Users with shunt/mixing valves can set `max_tank < max_lwt`.

- Config flow restructured: step 1 is now "System Setup" (operation mode,
  HP type, refrigerant).  Existing installations will need to reconfigure
  to set the new fields (defaults to ASHP / R290 / Full MPC).
