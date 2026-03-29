# Heat Pump MPC for Home Assistant

> **STRICT WARNING:** This is an early alpha version. Using this integration will potentially "brick your heating system". There will be many breaking changes during development. Use at your own risk!

This is a Home Assistant integration that calculates the most cost-effective running schedule for your heat pump. It works in tandem with *Heating Analytics* to understand your house's heat demand, and then determines when the heat pump should run, and with what leaving water temperature (LWT) and output level.

## Basic Concepts and Settings

To get the integration working optimally, it's important to understand the various concepts and settings:

### 1. Data Sources
The integration needs information about the world around it to plan ahead:
*   **Heating Analytics:** Tells how much heat (kWh) your house needs hour by hour.
*   **Electricity Price (Nordpool/Tibber):** Allows the integration to shift heating to the hours when electricity is cheapest.
*   **Weather Forecast:** Used to predict humidity, which is important for calculating when the heat pump must defrost (which lowers efficiency).
*   **Buffer Tank Temperature:** Tells the system how much heat is already stored in your tank.

### 2. Heat Pump & Tank
These are the physical constraints of your system:
*   **Nominal and Minimum Output (kW):** How much heat the heat pump provides at full throttle, and how low it can modulate down. The integration will evaluate both to find the cheapest option.
*   **Leaving Water Temperature (LWT) Constraints:** Absolute minimum and maximum temperature the water from the heat pump can have.
*   **Buffer Tank Volume:** The size of your tank in liters. This acts as a thermal battery. The integration can heat the tank extra before the electricity price goes up, as long as it does not exceed the **tank's safety ceiling**.
*   **DHW (Domestic Hot Water) Tank:** If your heat pump also heats tap water, you can define a DHW tank. The integration can schedule DHW runs during cheap hours, avoiding periods where electricity is expensive, while ensuring the tank never falls below a minimum temperature. You can also specify "ready-by" times for when the tank must be fully heated.
*   **Heating Curve and Room Temperature:** By setting the desired temperature at -10 °C and +10 °C outside, the system understands how hot the water needs to be to keep the house at the desired indoor temperature. This prevents the integration from choosing an unrealistically low and efficient temperature that cannot heat the house.

### 3. COP Learning & Calibration
Instead of just guessing how efficient your heat pump is based on a datasheet, this integration can *learn* its actual efficiency (COP - Coefficient of Performance).
*   By connecting sensors for **power consumption** (electrical energy) and **delivered heat** (thermal power via a heat meter, or flow meter and supply/return temperatures), the system will continuously adjust its expectations.
*   Capacity derating at low outdoor temperatures is learned implicitly: whenever the MPC decides to run at full output, the resulting heat delivery is used to update how much the pump can actually deliver at those conditions. No compressor frequency sensor is required.

### 4. Optimization Tuning
*   **Horizon Hours:** How many hours ahead the system should plan. 24 hours is standard, but can be increased if the next day's electricity prices are available early.
*   **Tank Standby Loss:** An estimate of how much heat the buffer tank loses to the surroundings per hour.
*   **LWT Step:** How precisely the system should search for the best flow temperature. Smaller steps give more precision but require more computing power.
*   **Compressor Start Penalty:** Electrical energy (kWh) added to the cost of each compressor start event. Encourages fewer, longer run periods instead of many short cycles.

## Connecting to Your Heat Pump

**This integration is an advisory layer — it does not control your heat pump directly.**

The MPC calculates the optimal leaving water temperature (LWT) setpoint and output level and exposes them as Home Assistant entities:

| Entity | What it provides |
|---|---|
| `number.heat_pump_mpc_lwt_setpoint` | Recommended flow temperature (°C) |
| `sensor.heat_pump_mpc_optimal_output` | Recommended output level (kW) |
| `binary_sensor.heat_pump_mpc_pump_on` | Whether the pump should run this hour |
| `binary_sensor.heat_pump_mpc_dhw_mode_on` | Whether the pump should run in DHW mode this hour |
| `number.heat_pump_mpc_dhw_setpoint` | Recommended DHW setpoint (°C) |
| `sensor.heat_pump_mpc_optimal_flow_temperature` | Read-only mirror of the solver's recommended LWT (°C) |
| `sensor.heat_pump_mpc_estimated_cop` | Effective COP for the current hour |
| `sensor.heat_pump_mpc_projected_heating_cost` | Projected electricity cost over horizon |
| `sensor.heat_pump_mpc_next_run_start` | Timestamp of the next scheduled pump start |
| `sensor.heat_pump_mpc_sh_thermal_energy` | Cumulative space-heating thermal energy (kWh_th) |
| `binary_sensor.heat_pump_mpc_schedule_feasible` | True when the solver satisfied all constraints |

**You are responsible for building the control layer** that reads these entities and writes to your heat pump. This is intentional: heat pump interfaces vary wildly (Modbus RTU/TCP, proprietary APIs, climate entities, ESPAltherma, etc.), and blindly writing setpoints without understanding your specific unit's safety mechanisms can trigger fault codes or lockouts.

### Recommended approach

1. Read `binary_sensor.heat_pump_mpc_pump_on` and `binary_sensor.heat_pump_mpc_dhw_mode_on` to decide whether to enable the pump and which mode it should be in.
2. If DHW mode is on, write `number.heat_pump_mpc_dhw_setpoint` to the heat pump's DHW setpoint. (When DHW is not scheduled, writing the lower setpoint blocks unsolicited reheating).
3. If SH (Space Heating) mode is on, read `number.heat_pump_mpc_lwt_setpoint` and write it to your pump's flow temperature setpoint *only when the value is within your pump's safe operating range*.
4. Always implement a **safety clamp** in your automation: never write a value outside the bounds you have verified are safe for your specific installation (mixing valves, underfloor heating limits, DHW priority, etc.).
5. Do not write setpoints during defrost cycles — let the pump manage those itself.

The integration runs every 30 minutes. A simple HA automation triggered on state change of the relevant entities is sufficient for most setups.
