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
