import pytest
from core.heat_pump_model import HeatPumpModel, get_profile, PROFILES, DEFAULT_PROFILE

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


def test_get_cop_at_output_capacity_limited():
    """O4: When physical capacity drops below min_output_kw, COP must be
    full-load COP — not the (better) min-load COP.

    At extreme cold the capacity ceiling can fall below the minimum inverter
    output.  The solver passes max_cap as max_output_kw; when max_cap ≤ min_kw
    the model must return full-load COP because the compressor is running at
    100% of its available capacity (no part-load regime exists).
    """
    model = HeatPumpModel()
    t_out = -20.0
    rh = 60.0
    lwt = 35.0

    # Simulate capacity-limited scenario: max_cap < min_output_kw
    max_cap = 2.6  # physical ceiling at −20 °C
    min_kw = 4.37  # inverter minimum (higher than capacity!)
    eff = max_cap   # actual output = capacity ceiling

    cop = model.get_cop_at_output(t_out, rh, lwt, eff, max_cap, min_kw)
    cop_full = model.get_effective_cop(t_out, rh, lwt)

    # Must return full-load COP (not min-load with modulation gain)
    assert abs(cop - cop_full) < 1e-4

    # Contrast: at mild conditions with headroom, min-load COP should be better
    cop_mild_min = model.get_cop_at_output(7.0, rh, lwt, 2.0, 5.0, 2.0)
    cop_mild_full = model.get_effective_cop(7.0, rh, lwt)
    assert cop_mild_min > cop_mild_full  # modulation gain active

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


# ---------------------------------------------------------------------------
# Profile-based model tests
# ---------------------------------------------------------------------------


def test_get_profile_known():
    """get_profile returns the correct profile for known combinations."""
    p = get_profile("ashp", "r290")
    assert p["has_defrost"] is True
    assert p["eta_carnot_default"] == 0.42

    p = get_profile("gshp", "r290")
    assert p["has_defrost"] is False
    assert p["eta_carnot_default"] == 0.45


def test_get_profile_unknown_falls_back():
    """Unknown combinations fall back to ASHP R290."""
    p = get_profile("unknown_type", "unknown_gas")
    assert p is DEFAULT_PROFILE


def test_get_profile_case_insensitive():
    """Profile lookup is case-insensitive."""
    p = get_profile("ASHP", "R290")
    assert p["has_defrost"] is True


def test_gshp_no_defrost():
    """GSHP profile disables defrost penalty entirely."""
    profile = get_profile("gshp", "r290")
    model = HeatPumpModel(profile)

    # Even at cold + humid conditions, GSHP should return 1.0 (no penalty)
    assert model._get_defrost_penalty(2.0, 90.0) == 1.0
    assert model._get_defrost_penalty(-5.0, 95.0) == 1.0

    # ASHP would penalise these conditions
    ashp = HeatPumpModel(get_profile("ashp", "r290"))
    assert ashp._get_defrost_penalty(2.0, 90.0) < 1.0


def test_gshp_higher_cop_at_same_lift():
    """GSHP should have higher COP than ASHP at the same thermal lift.

    At lift = 30 K both types should produce a COP, but GSHP's curve
    is shifted higher (better η_Carnot from stable source).
    """
    ashp = HeatPumpModel(get_profile("ashp", "r290"))
    gshp = HeatPumpModel(get_profile("gshp", "r290"))

    lift = 30.0
    cop_ashp = ashp._get_base_cop(lift)
    cop_gshp = gshp._get_base_cop(lift)
    assert cop_gshp > cop_ashp


def test_gshp_flat_capacity_curve():
    """GSHP capacity should be nearly flat across brine temperature range."""
    model = HeatPumpModel(get_profile("gshp", "r290"))

    cap_cold = model.get_max_output_at(0.0, 35.0, 10.0)
    cap_warm = model.get_max_output_at(5.0, 35.0, 10.0)

    # Less than 10% variation in the 0–5°C brine range
    assert abs(cap_warm - cap_cold) / cap_warm < 0.10


def test_all_profiles_produce_valid_model():
    """Every registered profile should produce a working HeatPumpModel."""
    for (hp_type, refrig), profile in PROFILES.items():
        model = HeatPumpModel(profile)
        cop = model.get_effective_cop(5.0, 60.0, 35.0)
        assert cop > 1.0, f"Profile ({hp_type}, {refrig}) produced COP <= 1.0"
        cap = model.get_max_output_at(5.0, 35.0, 10.0)
        assert cap > 0.0, f"Profile ({hp_type}, {refrig}) produced zero capacity"
