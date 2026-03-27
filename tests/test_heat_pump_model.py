import pytest
from core.heat_pump_model import HeatPumpModel

def test_init():
    model = HeatPumpModel()
    assert len(model._cop_curve) > 0
    assert len(model._modulation_gain_curve) > 0
    assert len(model._capacity_temp_curve) > 0
    assert len(model._capacity_lwt_curve) > 0

def test_calculate_lift():
    model = HeatPumpModel()
    assert model._calculate_lift(7.0, 35.0) == 28.0
    assert model._calculate_lift(-7.0, 35.0) == 42.0

def test_get_base_cop():
    model = HeatPumpModel()
    # Test known points
    assert abs(model._get_base_cop(28.0) - 4.42) < 1e-4
    assert abs(model._get_base_cop(38.0) - 3.64) < 1e-4
    assert abs(model._get_base_cop(42.0) - 3.33) < 1e-4

    # Test interpolation
    cop_33 = model._get_base_cop(33.0)
    assert 3.64 < cop_33 < 4.42

    # Test extrapolation below
    cop_low = model._get_base_cop(20.0)
    assert cop_low > 4.42

    # Test extrapolation above
    cop_high = model._get_base_cop(50.0)
    assert cop_high < 3.33

def test_get_defrost_penalty():
    model = HeatPumpModel()
    assert model._get_defrost_penalty(6.0, 75.0) == 0.85
    assert model._get_defrost_penalty(8.0, 75.0) == 1.0
    assert model._get_defrost_penalty(6.0, 60.0) == 1.0

def test_get_modulation_gain():
    model = HeatPumpModel()
    assert abs(model._get_modulation_gain(28.0) - 1.269) < 1e-4
    assert abs(model._get_modulation_gain(38.0) - 1.173) < 1e-4

    # Interpolation
    gain_mid = model._get_modulation_gain(33.0)
    assert 1.173 < gain_mid < 1.269

def test_get_cop_at_output():
    model = HeatPumpModel()
    t_out = 7.0
    rh = 60.0
    lwt = 35.0
    max_out = 5.0
    min_out = 2.0

    # Full load should match effective COP
    full_load_cop = model.get_cop_at_output(t_out, rh, lwt, max_out, max_out, min_out)
    eff_cop = model.get_effective_cop(t_out, rh, lwt)
    assert abs(full_load_cop - eff_cop) < 1e-4

    # Min load should have modulation gain applied
    min_load_cop = model.get_cop_at_output(t_out, rh, lwt, min_out, max_out, min_out)
    lift = lwt - t_out
    gain = model._get_modulation_gain(lift)
    assert abs(min_load_cop - eff_cop * gain) < 1e-4

    # Mid load should be interpolated
    mid_load_cop = model.get_cop_at_output(t_out, rh, lwt, 3.5, max_out, min_out)
    assert eff_cop < mid_load_cop < min_load_cop

def test_apply_learned_capacity():
    model = HeatPumpModel()
    original_minus_7 = next(f for t, f in model._capacity_temp_curve if t == -7.0)

    anchors = [
        (-7.0, 0.90, True),
        (-15.0, 0.80, False) # Should not be applied
    ]
    model.apply_learned_capacity(anchors)

    new_minus_7 = next(f for t, f in model._capacity_temp_curve if t == -7.0)
    assert new_minus_7 == 0.90

    # -15 should be unchanged because reliable=False
    new_minus_15 = next(f for t, f in model._capacity_temp_curve if t == -15.0)
    assert new_minus_15 == 0.63

def test_interp_1d():
    curve = [(0.0, 0.0), (10.0, 10.0)]
    assert HeatPumpModel._interp_1d(curve, 5.0) == 5.0
    assert HeatPumpModel._interp_1d(curve, -5.0) == 0.0 # Clamped low
    assert HeatPumpModel._interp_1d(curve, 15.0) == 15.0 # Extrapolated

def test_get_max_output_at():
    model = HeatPumpModel()

    # Base case A7/W35
    assert abs(model.get_max_output_at(7.0, 35.0, 5.0) - 5.0) < 1e-4

    # Colder outdoor
    assert model.get_max_output_at(-7.0, 35.0, 5.0) < 5.0

    # Higher LWT
    assert model.get_max_output_at(7.0, 45.0, 5.0) < 5.0

def test_get_effective_cop():
    model = HeatPumpModel()

    # Clean conditions
    cop_clean = model.get_effective_cop(7.0, 60.0, 35.0)
    assert abs(cop_clean - 4.42) < 1e-4

    # Defrost conditions
    cop_defrost = model.get_effective_cop(5.0, 80.0, 35.0)
    assert cop_defrost < model._get_base_cop(30.0) # 35 - 5 = 30 lift
