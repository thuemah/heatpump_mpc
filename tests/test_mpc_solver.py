import pytest
from core.mpc_solver import MpcSolver, HorizonPoint, MpcConfig, _tank_kwh_per_k, _max_tank_energy, _lwt_candidates, _output_candidates
from core.heat_pump_model import HeatPumpModel

class MockHeatPumpModel(HeatPumpModel):
    def get_effective_cop(self, t_outdoor, rh, lwt):
        return 3.0

    def get_max_output_at(self, t_outdoor, lwt, rated_kw):
        return rated_kw

    def get_cop_at_output(self, t_outdoor, rh, lwt, output_kw, max_output_kw, min_output_kw):
        return 3.0

@pytest.fixture
def solver():
    return MpcSolver(MockHeatPumpModel())

@pytest.fixture
def config():
    return MpcConfig(
        min_lwt=35.0,
        max_lwt=55.0,
        max_tank_temp=55.0,
        heat_pump_output_kw=5.0,
        tank_volume_liters=300.0,
        lwt_step=5.0,
        tank_standby_loss_kwh=0.05,
        min_output_kw=2.0
    )

def test_cost_per_kwh_heat(solver):
    cost = solver.cost_per_kwh_heat(1.5, 7.0, 60.0, 35.0)
    assert abs(cost - 0.5) < 1e-4 # 1.5 / 3.0

def test_solve_empty_horizon(solver, config):
    with pytest.raises(ValueError):
        solver.solve([], 40.0, config)

def test_solve_feasible(solver, config):
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0),
        HorizonPoint(price=2.0, t_outdoor=5.0, rh=60.0, house_demand=1.0),
        HorizonPoint(price=0.5, t_outdoor=5.0, rh=60.0, house_demand=1.0)
    ]
    # Tank initially at 40C, which is 5C above min_lwt (35C)
    # Energy = 5 * 300 * 0.00116 = 1.74 kWh
    result = solver.solve(horizon, 40.0, config)

    assert result.feasible
    assert len(result.schedule) == 3
    # With a simple mock model returning COP 3 and max_output 5
    # The solver will find a valid schedule

def test_solve_infeasible(solver, config):
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=10.0), # Too high demand
        HorizonPoint(price=2.0, t_outdoor=5.0, rh=60.0, house_demand=10.0)
    ]
    # Tank initially at min_lwt (no energy)
    result = solver.solve(horizon, 35.0, config)

    assert not result.feasible
    assert len(result.schedule) == 2

def test_tank_kwh_per_k(config):
    assert abs(_tank_kwh_per_k(config) - (300.0 * 1.16e-3)) < 1e-4

def test_max_tank_energy(config):
    # 300 * 1.16e-3 * (55 - 35) = 6.96 kWh
    expected = 300.0 * 1.16e-3 * 20.0
    assert abs(_max_tank_energy(config) - expected) < 1e-4

def test_lwt_candidates(config):
    candidates = _lwt_candidates(config)
    assert candidates == [35.0, 40.0, 45.0, 50.0, 55.0]

def test_output_candidates(config):
    candidates = _output_candidates(config)
    assert candidates == [2.0, 5.0]

    # Test identical min/max
    config.min_output_kw = 5.0
    assert _output_candidates(config) == [5.0]

def test_simulate(solver, config):
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0)
    ]
    metrics = [
        {"effective_output_kw": 5.0, "max_capacity_kw": 5.0},
        {"effective_output_kw": 5.0, "max_capacity_kw": 5.0}
    ]
    pump_on = [True, False]

    # Initial energy = 0
    states, feasible = solver._simulate(horizon, metrics, pump_on, config, 0.0)

    assert feasible
    assert len(states) == 2

    # Hour 1: tank starts 0, +5 in, -1 demand, -0.05 loss = 3.95 end
    assert abs(states[0]["tank_start"] - 0.0) < 1e-4
    assert abs(states[0]["heat_in"] - 5.0) < 1e-4
    assert abs(states[0]["tank_end"] - 3.95) < 1e-4

    # Hour 2: tank starts 3.95, +0 in, -1 demand, -0.05 loss = 2.9 end
    assert abs(states[1]["tank_start"] - 3.95) < 1e-4
    assert abs(states[1]["heat_in"] - 0.0) < 1e-4
    assert abs(states[1]["tank_end"] - 2.9) < 1e-4


def test_simulate_lwt_charging_ceiling(solver, config):
    """Pump cannot charge a tank that is already at or above the LWT.

    If the tank starts at 50 °C and LWT = 40 °C, the pump delivers nothing
    because heat cannot flow from 40 °C water into a 50 °C tank.
    """
    # config: min_lwt=35, max_tank_temp=55, volume=300 L → kwh_per_k=0.348
    # Tank at 50 °C: energy = (50-35) * 0.348 = 5.22 kWh
    kwh_per_k = 300.0 * 1.16e-3
    tank_at_50c = (50.0 - 35.0) * kwh_per_k  # 5.22 kWh

    horizon = [HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=0.5)]
    metrics = [{"effective_output_kw": 5.0, "max_capacity_kw": 5.0}]
    pump_on = [True]

    # LWT = 40 °C — tank is already above 40 °C, so no heat can be added.
    # max_charge_energy = (40-35) * 0.348 = 1.74 kWh
    # headroom = 1.74 - 5.22 = negative → heat_in = 0
    states, _ = solver._simulate(horizon, metrics, pump_on, config, tank_at_50c, lwt=40.0)
    assert states[0]["heat_in"] == 0.0

    # LWT = 55 °C — pump can deliver up to max_tank_temp; headroom exists.
    states_high, _ = solver._simulate(horizon, metrics, pump_on, config, tank_at_50c, lwt=55.0)
    assert states_high[0]["heat_in"] > 0.0


def test_simulate_low_mass_pass_through(solver):
    """Pass-through heat delivery for direct-coupled / low-mass systems.

    When a tiny buffer tank (50 L) saturates at the LWT ceiling within
    minutes at full output, the old model blocked all subsequent charging
    because headroom dropped to zero.  The fix accounts for demand drain
    during the hour, allowing the pump to deliver heat continuously even
    when the tank starts the hour right at the ceiling.

    The physics invariant is preserved: when the tank temperature is
    *above* the LWT (pump water cooler than tank), heat_in must still be
    zero — heat cannot flow uphill.
    """
    small_config = MpcConfig(
        min_lwt=25.0,
        max_lwt=45.0,
        max_tank_temp=45.0,
        heat_pump_output_kw=12.0,
        tank_volume_liters=50.0,
        lwt_step=4.0,
        tank_standby_loss_kwh=0.05,
        min_output_kw=3.0,
    )
    kwh_per_k = 50.0 * 1.16e-3  # 0.058 kWh/K

    # Tank already at the LWT ceiling (29 °C → max_charge_energy for LWT=29).
    # max_charge_energy = (29-25)*0.058 = 0.232 kWh
    tank_at_ceiling = (29.0 - 25.0) * kwh_per_k  # 0.232 kWh

    house_demand = 1.5  # kWh/h — cannot be met by stored energy alone
    horizon = [HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=house_demand)]
    metrics = [{"effective_output_kw": 12.0, "max_capacity_kw": 12.0}]
    pump_on = [True]

    states, _ = solver._simulate(horizon, metrics, pump_on, small_config, tank_at_ceiling, lwt=29.0)

    # With pass-through the pump delivers demand + standby even though the
    # tank started at the ceiling.  heat_in = min(12, 0 + 1.5 + 0.05) = 1.55
    assert abs(states[0]["heat_in"] - (house_demand + small_config.tank_standby_loss_kwh)) < 1e-6
    # Tank ends exactly at the ceiling (demand was fully met).
    assert abs(states[0]["tank_end"] - tank_at_ceiling) < 1e-6

    # Physics preserved: tank ABOVE LWT → heat_in must be zero.
    # Force tank above the LWT=29 ceiling and verify the pump is blocked.
    tank_above_ceiling = tank_at_ceiling + 0.5  # above ceiling
    states_blocked, _ = solver._simulate(
        horizon, metrics, pump_on, small_config, tank_above_ceiling, lwt=29.0
    )
    assert states_blocked[0]["heat_in"] == 0.0


def test_solve_low_mass_system_feasible(solver):
    """End-to-end: solver must produce a feasible schedule for a low-mass system.

    Reproduces the Rachid fan-coil scenario: 50 L buffer, demand of ~1.6 kWh/h
    for 24 hours.  With the charging-ceiling fix the solver must be able to
    schedule enough pump-on hours to cover the demand and return feasible=True.
    """
    small_config = MpcConfig(
        min_lwt=25.0,
        max_lwt=45.0,
        max_tank_temp=45.0,
        heat_pump_output_kw=12.0,
        tank_volume_liters=50.0,
        lwt_step=4.0,
        tank_standby_loss_kwh=0.05,
        min_output_kw=3.0,
    )
    horizon = [
        HorizonPoint(price=float(1 + (t % 3)), t_outdoor=5.0, rh=60.0, house_demand=1.6)
        for t in range(24)
    ]
    result = solver.solve(horizon, 25.5, small_config)

    assert result.feasible
    # Pump must run for multiple hours to cover the demand.
    assert sum(1 for p in result.schedule if p.pump_on) > 1


def test_solve_rejects_lwt_below_tank_temp(solver, config):
    """Solver must not choose an LWT below the current tank temperature
    when the pump is forced to run.

    Tank at 50 °C (5.22 kWh stored above min_lwt=35).  With 2 kWh/h demand
    for 5 hours the tank cannot coast — the pump MUST run.  LWT candidates
    below ~45 °C cannot deliver enough heat (charging ceiling too low), so
    the solver must pick LWT > 35 °C.
    """
    tank_init_temp = 50.0  # °C — tank starts warm
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=2.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=2.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=2.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=2.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=2.0),
    ]
    result = solver.solve(horizon, tank_init_temp, config)

    # At LWT=35 (min_lwt) max_charge_energy=(35-35)*kwh_per_k=0: no heat possible.
    # The solver must choose a higher LWT to make the schedule feasible.
    assert result.feasible
    assert result.optimal_lwt > 35.0
    # The pump must have been on for at least one hour.
    assert any(p.pump_on for p in result.schedule)


def test_solve_respects_emission_constraint(solver):
    """Solver must skip LWT candidates that cannot deliver peak demand to the building.

    With k_emission=0.2 kW/K and t_room=21 °C, delivering 4 kWh/h requires:
        LWT_min = 21 + 4.0 / 0.2 = 41 °C
    Candidates 35 °C and 40 °C are filtered out; the solver must choose ≥ 45 °C.

    Note: this constraint is purely thermal — house_demand is thermal kWh/h,
    k_emission is thermal kW/K.  No electrical energy involved here.
    """
    config = MpcConfig(
        min_lwt=35.0,
        max_lwt=55.0,
        max_tank_temp=55.0,
        heat_pump_output_kw=5.0,
        tank_volume_liters=300.0,
        lwt_step=5.0,
        tank_standby_loss_kwh=0.05,
        min_output_kw=2.0,
        k_emission=0.2,   # thermal kW/K
        t_room=21.0,      # °C
    )
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=4.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=4.0),
    ]
    result = solver.solve(horizon, 40.0, config)

    # LWT candidates [35, 40] filtered; solver must pick ≥ 45 °C
    assert result.optimal_lwt >= 45.0


def test_start_penalty_adds_cost(solver, config):
    """Start penalty is charged once per off→on transition and raises total cost.

    With price=1.0 and start_penalty_kwh=1.0, a schedule with one start adds
    exactly 1.0 currency to total_cost compared to the same schedule without
    a penalty.  start_event must be True only on the first hour of each cycle.
    """
    config_no_penalty = MpcConfig(
        min_lwt=35.0, max_lwt=55.0, max_tank_temp=55.0,
        heat_pump_output_kw=5.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.0, min_output_kw=2.0,
        start_penalty_kwh=0.0,
    )
    config_with_penalty = MpcConfig(
        min_lwt=35.0, max_lwt=55.0, max_tank_temp=55.0,
        heat_pump_output_kw=5.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.0, min_output_kw=2.0,
        start_penalty_kwh=1.0,
    )
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0),
    ]
    r_no = solver.solve(horizon, 35.0, config_no_penalty)
    r_with = solver.solve(horizon, 35.0, config_with_penalty)

    # Count starts in the penalty schedule
    starts = sum(1 for p in r_with.schedule if p.start_event)
    assert starts >= 1

    # total_cost difference equals number of starts × penalty × price
    cost_diff = r_with.total_cost - r_no.total_cost
    assert abs(cost_diff - starts * 1.0) < 1e-6

    # start_event True only on first hour of each run, not on continuation hours
    for i, p in enumerate(r_with.schedule):
        if p.start_event:
            assert p.pump_on
            assert i == 0 or not r_with.schedule[i - 1].pump_on


def test_dhw_phase1_picks_cheapest_hour():
    """O6: DHW Phase 1 should pick the cheapest hour (price/COP), not the latest.

    Given candidate hours before a DHW tank violation — one cheap and several
    expensive — Phase 1 should include the cheaper one in its schedule.
    """
    config = MpcConfig(
        min_lwt=35.0, max_lwt=55.0, max_tank_temp=55.0,
        heat_pump_output_kw=9.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.05, min_output_kw=3.0,
        dhw_enabled=True, dhw_tank_volume_liters=180.0,
        dhw_min_temp=40.0, dhw_target_temp=55.0, dhw_lwt=55.0,
    )
    # Moderate DHW demand — enough to need 1-2 hours of DHW, not all of them.
    dhw_demand = 0.5  # kWh/h
    horizon = [
        HorizonPoint(price=5.0, t_outdoor=5.0, rh=60.0, house_demand=0.5, dhw_demand=dhw_demand),
        HorizonPoint(price=0.1, t_outdoor=5.0, rh=60.0, house_demand=0.5, dhw_demand=dhw_demand),  # very cheap
        HorizonPoint(price=5.0, t_outdoor=5.0, rh=60.0, house_demand=0.5, dhw_demand=dhw_demand),
        HorizonPoint(price=5.0, t_outdoor=5.0, rh=60.0, house_demand=0.5, dhw_demand=dhw_demand),
        HorizonPoint(price=5.0, t_outdoor=5.0, rh=60.0, house_demand=0.5, dhw_demand=dhw_demand),
        HorizonPoint(price=5.0, t_outdoor=5.0, rh=60.0, house_demand=0.5, dhw_demand=dhw_demand),
    ]

    solver = MpcSolver(MockHeatPumpModel())
    # DHW tank starts at 45 °C → 1.044 kWh above min.  With 0.5 kWh/h demand,
    # the tank survives ~2 hours before depleting, so the violation occurs at
    # hour 2 — giving Phase 1 candidates [0, 1, 2] to choose from.
    result = solver.solve(horizon, 45.0, config, dhw_tank_temp_init=45.0)

    # Find which hours were scheduled for DHW
    dhw_hours = [p.hour_index for p in result.schedule if p.dhw_on]
    assert len(dhw_hours) >= 1, "DHW Phase 1 should have scheduled at least one hour."
    # The cheap hour (index 1, price=0.1) should be preferred over index 0 (price=5.0).
    assert 1 in dhw_hours, (
        f"DHW Phase 1 did not pick the cheapest hour (index 1, price=0.1). "
        f"Scheduled DHW at hours: {dhw_hours}"
    )


def test_capacity_limited_cop_not_inflated():
    """O4: At extreme cold where capacity < min_output, the solver must not
    assign min-load (best) COP to what is actually a full-load operating point.

    We use a real HeatPumpModel (not mock) to verify the fix end-to-end.
    At −20 °C the capacity drops to ~2.6 kW (52% of 5 kW rated).  With
    min_output_kw=4.37, the pump is capacity-constrained.  The COP for
    a scheduled hour must equal the full-load COP, not the (better) min-load COP.
    """
    model = HeatPumpModel()
    solver = MpcSolver(model)
    config = MpcConfig(
        min_lwt=35.0, max_lwt=35.0, max_tank_temp=55.0,
        heat_pump_output_kw=5.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.05,
        min_output_kw=4.37,
    )
    # Single hour at −20 °C with enough demand to force the pump on.
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=-20.0, rh=60.0, house_demand=2.0),
    ]
    result = solver.solve(horizon, 40.0, config)

    # The pump must have run.
    assert result.schedule[0].pump_on or result.schedule[0].heat_delivered_kwh > 0

    # COP should be the full-load value, NOT inflated by modulation gain.
    cop_full = model.get_effective_cop(-20.0, 60.0, 35.0)
    cop_scheduled = result.schedule[0].cop_effective
    # Allow small tolerance — but it must NOT be significantly higher than full-load.
    assert cop_scheduled <= cop_full + 0.05, (
        f"COP {cop_scheduled:.3f} exceeds full-load COP {cop_full:.3f} — "
        f"modulation gain incorrectly applied at capacity-limited conditions."
    )


def test_sh_dhw_conflict_resolution():
    """O2: When DHW Phase 1 blocks hours that SH needs, the solver retries
    without DHW and produces a feasible SH schedule.

    Setup: 4 hours of moderate SH demand with a 300 L tank (min_lwt=35,
    max=55).  DHW Phase 1 books 2 hours for a ready-by constraint.  With
    only 2 remaining hours for SH the schedule is infeasible.  The conflict
    resolution drops DHW and retries — all 4 hours become available for SH,
    making the schedule feasible.
    """
    config = MpcConfig(
        min_lwt=35.0, max_lwt=55.0, max_tank_temp=55.0,
        heat_pump_output_kw=5.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.05, min_output_kw=2.0,
        dhw_enabled=True, dhw_tank_volume_liters=180.0,
        dhw_min_temp=40.0, dhw_target_temp=55.0, dhw_lwt=55.0,
        dhw_ready_by_hours=[1],  # DHW must be ready at hour 1
    )
    # SH demand needs 3+ of the 4 hours to stay feasible.
    demand = 4.0
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=demand, dhw_demand=1.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=demand, dhw_demand=1.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=demand, dhw_demand=1.0),
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=demand, dhw_demand=1.0),
    ]

    solver = MpcSolver(MockHeatPumpModel())
    # SH tank at 45 °C (some stored energy); DHW tank starts low.
    result = solver.solve(horizon, 45.0, config, dhw_tank_temp_init=41.0)

    # The solver must produce a feasible schedule (DHW may be dropped).
    assert result.feasible, (
        "SH/DHW conflict resolution failed — schedule should be feasible "
        "after dropping DHW constraints."
    )


def test_coil_in_tank_drains_tank_faster():
    """Coil-in-tank: spiral demand drains the SH tank alongside house demand.

    Simulate the tank with and without coil demand using the same pump
    schedule.  The coil scenario must end with lower tank energy because
    the spiral draws additional heat.
    """
    config_coil = MpcConfig(
        min_lwt=35.0, max_lwt=55.0, max_tank_temp=55.0,
        heat_pump_output_kw=5.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.05, min_output_kw=2.0,
        coil_in_tank=True,
    )
    config_no_coil = MpcConfig(
        min_lwt=35.0, max_lwt=55.0, max_tank_temp=55.0,
        heat_pump_output_kw=5.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.05, min_output_kw=2.0,
        coil_in_tank=False,
    )
    horizon_coil = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0, dhw_demand=0.5)
        for _ in range(4)
    ]
    horizon_no = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0, dhw_demand=0.5)
        for _ in range(4)
    ]
    metrics = [{"effective_output_kw": 5.0, "max_capacity_kw": 5.0}] * 4
    pump_on = [True, False, False, False]
    kwh_per_k = 300 * 1.16e-3
    init_energy = (45.0 - 35.0) * kwh_per_k  # 3.48 kWh

    solver = MpcSolver(MockHeatPumpModel())
    states_coil, _ = solver._simulate(horizon_coil, metrics, pump_on, config_coil, init_energy, lwt=55.0)
    states_no, _ = solver._simulate(horizon_no, metrics, pump_on, config_no_coil, init_energy, lwt=55.0)

    # With coil: tank drains 0.5 kWh/h faster.
    for t in range(4):
        assert states_coil[t]["tank_end"] < states_no[t]["tank_end"], (
            f"Hour {t}: coil tank_end ({states_coil[t]['tank_end']:.2f}) "
            f"should be lower than no-coil ({states_no[t]['tank_end']:.2f})."
        )


def test_coil_in_tank_ready_by_constraint():
    """Coil-in-tank ready-by: SH tank must hold reserve energy at specified hours.

    With coil_reserve_kwh = 2.0 and ready-by at hour 2, the solver must
    ensure the SH tank has >= 2.0 kWh at the end of hour 2.
    """
    config = MpcConfig(
        min_lwt=35.0, max_lwt=55.0, max_tank_temp=55.0,
        heat_pump_output_kw=5.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.05, min_output_kw=2.0,
        coil_in_tank=True,
        coil_ready_by_hours=[2],
        coil_reserve_kwh=2.0,
    )
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.5, dhw_demand=0.3)
        for _ in range(6)
    ]

    solver = MpcSolver(MockHeatPumpModel())
    # Start with low tank energy so the solver must charge to meet reserve.
    result = solver.solve(horizon, 37.0, config)

    assert result.feasible, "Schedule should be feasible with coil reserve constraint."
    # Check that tank energy at end of hour 2 meets the reserve.
    tank_at_ready = result.schedule[2].tank_energy_kwh
    assert tank_at_ready >= 2.0 - 0.1, (
        f"Tank energy at ready-by hour 2 is {tank_at_ready:.2f} kWh, "
        f"expected >= 2.0 kWh (coil_reserve_kwh)."
    )


def test_coil_no_mode_switching():
    """Coil-in-tank must never schedule DHW mode hours — always SH.

    Even with dhw_demand > 0, coil_in_tank mode must not produce any
    dhw_on=True hours because there is no separate DHW mode.
    """
    config = MpcConfig(
        min_lwt=35.0, max_lwt=55.0, max_tank_temp=55.0,
        heat_pump_output_kw=5.0, tank_volume_liters=300.0,
        lwt_step=5.0, tank_standby_loss_kwh=0.05, min_output_kw=2.0,
        coil_in_tank=True,
        # dhw_enabled is False (coil mode, no separate tank)
    )
    horizon = [
        HorizonPoint(price=1.0, t_outdoor=5.0, rh=60.0, house_demand=1.0, dhw_demand=0.5)
        for _ in range(6)
    ]

    solver = MpcSolver(MockHeatPumpModel())
    result = solver.solve(horizon, 40.0, config)

    dhw_hours = sum(1 for p in result.schedule if p.dhw_on)
    assert dhw_hours == 0, (
        f"Coil mode should never schedule DHW hours, but found {dhw_hours}."
    )


def test_dhw_phase2_does_not_starve_sh():
    """DHW Phase 2 must not book every hour for DHW maintenance.

    Reproduces the bug where a small DHW tank with high daily demand (~10 kWh/day)
    caused Phase 2 to schedule DHW for ALL available hours because each hour had
    tiny demand-drain headroom (heat_in ≈ demand > 0).  The SH pump was left with
    zero available hours, draining the SH tank to negative → infeasible schedule.

    After the fix, Phase 2 only keeps hours that deliver net-positive energy to
    the DHW tank (heat_in > dhw_demand), leaving enough slots for SH.
    """
    config = MpcConfig(
        min_lwt=25.0,
        max_lwt=55.0,
        max_tank_temp=55.0,
        heat_pump_output_kw=9.0,
        tank_volume_liters=300.0,
        lwt_step=5.0,
        tank_standby_loss_kwh=0.05,
        min_output_kw=3.0,
        dhw_enabled=True,
        dhw_tank_volume_liters=180.0,
        dhw_min_temp=40.0,
        dhw_target_temp=55.0,
        dhw_lwt=55.0,
        # ~10.5 kWh/day — high relative to 180 L tank capacity (~3.1 kWh).
        # Per-hour demand = 10.512 / 24 ≈ 0.438 kWh, matching user's observed data.
    )
    # Inject DHW demand directly into horizon points.
    dhw_demand_per_hour = 10.512 / 24
    horizon = [
        HorizonPoint(
            price=float(1 + (t % 3) * 0.5),
            t_outdoor=5.0,
            rh=60.0,
            house_demand=1.7,
            dhw_demand=dhw_demand_per_hour,
        )
        for t in range(24)
    ]

    solver = MpcSolver(MockHeatPumpModel())
    # DHW tank starts at target; SH tank starts warm (48 °C).
    result = solver.solve(horizon, 48.0, config, dhw_tank_temp_init=55.0)

    # SH pump must have been scheduled for at least some hours.
    sh_hours = sum(1 for p in result.schedule if p.pump_on)
    assert sh_hours > 0, (
        f"SH pump was never scheduled — DHW Phase 2 likely blocked all hours "
        f"(dhw_on count: {sum(1 for p in result.schedule if p.dhw_on)})"
    )

    # DHW hours should be limited (not every single hour).
    dhw_hours = sum(1 for p in result.schedule if p.dhw_on)
    assert dhw_hours < len(horizon), (
        f"DHW scheduled for every hour ({dhw_hours}/{len(horizon)}), starving SH"
    )
