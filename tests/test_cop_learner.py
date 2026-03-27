import pytest
from core.cop_learner import (
    CopLearner,
    CopLearnerState,
    CopObservation,
    LearningResult,
    _carnot_cop,
    _nearest_capacity_anchor,
    _ema
)

def test_cop_learner_state_dict():
    state = CopLearnerState(eta_carnot=0.5)
    d = state.to_dict()
    assert d["eta_carnot"] == 0.5

    # Test from_dict ignoring unknown
    d["unknown_key"] = "test"
    new_state = CopLearnerState.from_dict(d)
    assert new_state.eta_carnot == 0.5

def test_carnot_cop():
    # lift = 35 - 7 = 28K. Thot = 35 + 273.15 = 308.15K
    # COP = 308.15 / 28 = 11.005
    cop = _carnot_cop(7.0, 35.0)
    assert abs(cop - 11.005) < 1e-3

    # Small lift returns None
    assert _carnot_cop(10.0, 11.0) is None

def test_nearest_capacity_anchor():
    assert _nearest_capacity_anchor(-15.0) == -15.0
    assert _nearest_capacity_anchor(-14.0) == -15.0
    assert _nearest_capacity_anchor(-7.0) == -7.0
    assert _nearest_capacity_anchor(-6.0) == -7.0

    # Outside radius
    assert _nearest_capacity_anchor(0.0) is None

def test_ema():
    # 10 * 0.9 + 20 * 0.1 = 9 + 2 = 11
    assert abs(_ema(10.0, 20.0, 0.1) - 11.0) < 1e-5

def test_is_reliable():
    state = CopLearnerState()
    learner = CopLearner(state)
    assert not learner.is_reliable

    state.eta_carnot_samples = 20
    assert learner.is_reliable

def test_is_capacity_reliable_at():
    state = CopLearnerState()
    learner = CopLearner(state)
    assert not learner.is_capacity_reliable_at(-15.0)

    state.capacity_minus15_samples = 5
    assert learner.is_capacity_reliable_at(-15.0)

    # Test invalid anchor
    assert not learner.is_capacity_reliable_at(0.0)

def test_predict_cop():
    state = CopLearnerState(eta_carnot=0.42, f_defrost=0.85)
    learner = CopLearner(state)

    # Clean condition
    cop_clean = learner.predict_cop(7.0, 60.0, 35.0)
    expected_carnot = _carnot_cop(7.0, 35.0)
    assert abs(cop_clean - (0.42 * expected_carnot)) < 1e-4

    # Defrost condition
    cop_defrost = learner.predict_cop(5.0, 80.0, 35.0)
    expected_carnot_defrost = _carnot_cop(5.0, 35.0)
    assert abs(cop_defrost - (0.42 * expected_carnot_defrost * 0.85)) < 1e-4

def test_observe_rejections():
    state = CopLearnerState()
    learner = CopLearner(state)

    # Zero elec
    obs = CopObservation(7.0, 60.0, 35.0, 3.0, 0.0)
    res = learner.observe(obs)
    assert not res.accepted
    assert "elec_kwh" in res.rejection_reason

    # Low heat
    obs = CopObservation(7.0, 60.0, 35.0, 0.01, 1.0)
    res = learner.observe(obs)
    assert not res.accepted
    assert "heat_out_kwh" in res.rejection_reason

    # Short duration
    obs = CopObservation(7.0, 60.0, 35.0, 3.0, 1.0, duration_hours=0.1)
    res = learner.observe(obs)
    assert not res.accepted
    assert "duration" in res.rejection_reason

    # Low lift
    obs = CopObservation(35.0, 60.0, 36.0, 3.0, 1.0)
    res = learner.observe(obs)
    assert not res.accepted
    assert "lift" in res.rejection_reason

    # Implausible COP
    obs = CopObservation(7.0, 60.0, 35.0, 10.0, 1.0) # COP 10
    res = learner.observe(obs)
    assert not res.accepted
    assert "COP_measured" in res.rejection_reason

def test_observe_clean_update():
    state = CopLearnerState(eta_carnot=0.42)
    learner = CopLearner(state, eta_learning_rate=0.1)

    # A7/W35, COP 4.42 -> eta ~ 0.402
    obs = CopObservation(7.0, 60.0, 35.0, 4.42, 1.0)
    res = learner.observe(obs)

    assert res.accepted
    assert res.eta_carnot_updated
    assert not res.f_defrost_updated
    assert not res.is_defrost_condition
    assert state.eta_carnot_samples == 1

    # 0.42 * 0.9 + 0.402 * 0.1 = 0.378 + 0.0402 = 0.4182
    expected_eta = 0.42 * 0.9 + (4.42 / _carnot_cop(7.0, 35.0)) * 0.1
    assert abs(state.eta_carnot - expected_eta) < 1e-4

def test_observe_defrost_update():
    state = CopLearnerState(eta_carnot=0.42, f_defrost=0.85)
    learner = CopLearner(state, defrost_learning_rate=0.1)

    # A5/W35, defrost conditions
    cop_expected = 0.42 * _carnot_cop(5.0, 35.0) * 0.80 # 0.80 instead of 0.85
    obs = CopObservation(5.0, 80.0, 35.0, cop_expected, 1.0)
    res = learner.observe(obs)

    assert res.accepted
    assert not res.eta_carnot_updated
    assert res.f_defrost_updated
    assert res.is_defrost_condition
    assert state.f_defrost_samples == 1

    # f_measured = 0.80. 0.85 * 0.9 + 0.80 * 0.1 = 0.845
    assert abs(state.f_defrost - 0.845) < 1e-4

def test_capacity_learning():
    state = CopLearnerState()
    learner = CopLearner(state)

    # Full-load observation near -7 °C anchor with ample tank headroom
    obs = CopObservation(-7.0, 60.0, 35.0, 3.9, 1.0,
                         is_full_load=True, rated_kw=5.0, tank_headroom_kwh=10.0)
    res = learner.observe(obs)

    assert res.capacity_updated
    assert res.capacity_anchor_c == -7.0
    # frac = 3.9 / 5.0 = 0.78
    assert res.capacity_frac_observed == 0.78
    assert state.capacity_minus7_samples == 1

def test_capacity_learning_skipped_when_not_full_load():
    state = CopLearnerState()
    learner = CopLearner(state)

    obs = CopObservation(-7.0, 60.0, 35.0, 3.9, 1.0,
                         is_full_load=False, rated_kw=5.0)
    res = learner.observe(obs)

    assert not res.capacity_updated
    assert state.capacity_minus7_samples == 0

def test_capacity_learning_skipped_when_tank_limiting():
    state = CopLearnerState()
    learner = CopLearner(state)

    # heat_out (3.9 kWh) >= headroom (4.0 kWh) * 0.9 → tank was limiting
    obs = CopObservation(-7.0, 60.0, 35.0, 3.9, 1.0,
                         is_full_load=True, rated_kw=5.0, tank_headroom_kwh=4.0)
    res = learner.observe(obs)

    assert not res.capacity_updated
    assert state.capacity_minus7_samples == 0

def test_diagnostics():
    state = CopLearnerState()
    learner = CopLearner(state)
    diag = learner.diagnostics()

    assert "eta_carnot" in diag
    assert "f_defrost" in diag
    assert "is_reliable" in diag
    assert diag["is_reliable"] is False
