"""
COP Learner — runtime calibration of heat pump efficiency.

Pure Python.  No Home Assistant dependencies.  Designed to be persisted
via HA storage and injected into the coordinator as a stateful object.

Physics background
------------------
Theoretical maximum COP for a heat pump (Carnot):

    COP_Carnot = T_hot_K / (T_hot_K − T_cold_K)

where T_hot = leaving water temperature (LWT) and T_cold = outdoor air
temperature, both in Kelvin.

Real heat pumps achieve a fixed fraction of the Carnot limit:

    COP_real = η_Carnot × COP_Carnot

η_Carnot ("second-law efficiency") is ~0.38–0.46 for modern inverter heat
pumps and is remarkably stable across the operating range of a single unit.
This makes it an ideal single-parameter calibration target: one number
corrects the entire COP curve.

Defrost cycles impose an additional penalty on η when the evaporator ices
up (typically T_outdoor < ~7 °C and RH > ~70 %):

    COP_effective = η_Carnot × COP_Carnot × f_defrost

f_defrost < 1.0 during icing conditions; 1.0 otherwise.

Learning strategy
-----------------
Every completed run cycle produces one observation:
    heat_out_kwh  — thermal energy delivered (from heat meter or flow × ΔT)
    elec_kwh      — electrical energy consumed

From these we compute:
    COP_measured  = heat_out / elec
    η_measured    = COP_measured / COP_Carnot(T_outdoor, LWT)

Observations are routed to one of two EMA tracks:

  Track "clean"   (no suspected icing) → update η_Carnot
  Track "defrost" (suspected icing)    → update f_defrost as
                                         η_measured / η_Carnot_current

The defrost track's ratio captures how much the penalty costs *relative to
the already-learned clean baseline*, so the two tracks are independent and
can be updated in any order.

Threshold learning (T_defrost, RH_defrost) is deferred to a future version
that requires sufficient bin coverage across the (T, RH) space.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Physical constants
# ---------------------------------------------------------------------------

_KELVIN_OFFSET: float = 273.15


# ---------------------------------------------------------------------------
# Guard rails — observations outside these bounds are rejected
# ---------------------------------------------------------------------------

# Physics: COP < 1 violates thermodynamics; COP > 7 is implausible for ASHP
_COP_MIN: float = 1.0
_COP_MAX: float = 7.0

# Lift < 5 K → pump at near-condensing conditions; COP unreliable
_LIFT_MIN_K: float = 5.0

# Observations shorter than this are too noisy (partial cycles, defrost start)
_MIN_DURATION_HOURS: float = 0.25   # 15 minutes

# Ignore run cycles with negligible heat output (standby, failed start)
_MIN_HEAT_KWH: float = 0.05


# ---------------------------------------------------------------------------
# Learning rates
# ---------------------------------------------------------------------------

# η_Carnot: slow — stable physical property; resist noise
DEFAULT_ETA_LEARNING_RATE: float = 0.04

# f_defrost: slightly faster — conditions vary and we have fewer samples
DEFAULT_DEFROST_LEARNING_RATE: float = 0.07

# Capacity: very slow — physically stable; outliers are expensive to undo
DEFAULT_CAPACITY_LEARNING_RATE: float = 0.03

# Minimum samples before learned values are considered reliable
MIN_RELIABLE_SAMPLES: int = 20
MIN_RELIABLE_CAPACITY_SAMPLES: int = 5

# Temperature anchors where capacity fractions are learned (°C).
# The 7 °C anchor is the EN 14511 rating point and is always 1.00 — no need to learn.
_CAPACITY_ANCHORS: tuple[float, ...] = (-15.0, -7.0)

# Maximum distance (°C) between an observation's outdoor temperature and
# an anchor for the observation to count toward that anchor.
_CAPACITY_BIN_RADIUS: float = 6.0


# ---------------------------------------------------------------------------
# Cold-start defaults — Sprsun R290 actual EN 14511 datasheet values
#
# η_Carnot per test point (T_hot = LWT, T_cold = T_outdoor):
#
#   A7/W35 rated:    COP 4.42  COP_Carnot = 308.15/28.0 = 11.005  η = 0.402
#   A7/W45 rated:    COP 3.64  COP_Carnot = 318.15/38.0 =  8.372  η = 0.435
#   A7/W35 min-load: COP 5.61  COP_Carnot = 308.15/28.0 = 11.005  η = 0.510
#
# The 0.48–0.51 range often cited for "inverter heat pumps" corresponds to
# part-load (min-power) operation.  At rated/full load the same machine
# achieves only η ≈ 0.40–0.44.
#
# MPC schedules binary on/off — the pump runs at rated output when on —
# so full-load η is the operationally correct cold-start value.
# The learner will refine this upward if the installation tends to run
# at partial load (e.g. mild weather, oversized unit).
#
# Mean rated η ≈ 0.42 → DEFAULT_ETA_CARNOT = 0.42.
# ---------------------------------------------------------------------------

DEFAULT_ETA_CARNOT: float = 0.42
DEFAULT_F_DEFROST: float = 0.85
DEFAULT_DEFROST_T_THRESHOLD: float = 7.0    # °C
DEFAULT_DEFROST_RH_THRESHOLD: float = 70.0  # %


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CopObservation:
    """
    A single completed run-cycle measurement used to update the model.

    Both ``heat_out_kwh`` and ``elec_kwh`` must cover the *same* time window.
    The coordinator is responsible for integrating instantaneous power or
    reading cumulative sensor deltas before constructing this object.
    """

    t_outdoor: float
    """Mean outdoor temperature during the run cycle (°C)."""

    rh: float
    """Mean relative humidity during the run cycle (%)."""

    lwt: float
    """Mean leaving water temperature during the run cycle (°C)."""

    heat_out_kwh: float
    """Thermal energy delivered to the system during the cycle (kWh).
    Source: heat meter, or flow-rate × ΔT × 1.163 integrated over time."""

    elec_kwh: float
    """Electrical energy consumed by the heat pump during the cycle (kWh)."""

    duration_hours: float = 1.0
    """Length of the observation window (hours).  Used as a quality guard."""

    rated_max_elec_kw: Optional[float] = None
    """Rated maximum electrical input power of the heat pump (kW), from the
    user's configuration.  When provided (> 0), the capacity-learning gate
    checks whether the measured electrical draw exceeded 90 % of this value,
    proving the compressor was running at full capacity regardless of who
    commanded it (MPC, thermostat, legionella cycle, etc.)."""

    rated_kw: Optional[float] = None
    """Rated full-load thermal output (kW) from the user's configuration.
    Required for capacity learning so the learner can compute the
    observed capacity fraction = actual_thermal_kw / rated_kw."""

    tank_headroom_kwh: Optional[float] = None
    """Available tank headroom at the start of the observation window (kWh).
    When provided, observations where headroom ≤ heat delivered are filtered
    out: the tank was the limiting factor, not the pump's capacity."""


@dataclass
class CopLearnerState:
    """
    All learned parameters.  Serialisable to/from a plain dict so HA
    storage can persist it across restarts without pickling.

    Both ``eta_carnot_samples`` and ``f_defrost_samples`` are raw counts
    (not weighted).  They drive the ``is_reliable`` check and provide a
    rough confidence indicator for the UI.
    """

    eta_carnot: float = DEFAULT_ETA_CARNOT
    """Learned Carnot efficiency factor (dimensionless, 0.3–0.6)."""

    f_defrost: float = DEFAULT_F_DEFROST
    """Learned defrost penalty factor (dimensionless, 0.6–1.0)."""

    defrost_t_threshold: float = DEFAULT_DEFROST_T_THRESHOLD
    """Outdoor temperature below which icing may occur (°C)."""

    defrost_rh_threshold: float = DEFAULT_DEFROST_RH_THRESHOLD
    """Relative humidity above which icing may occur (%)."""

    eta_carnot_samples: int = 0
    """Number of clean-condition observations used to update η_Carnot."""

    f_defrost_samples: int = 0
    """Number of defrost-condition observations used to update f_defrost."""

    # ------------------------------------------------------------------
    # Capacity derating — learned from max-frequency observations
    # ------------------------------------------------------------------

    capacity_frac_minus15: float = 0.63
    """Learned capacity fraction at −15 °C outdoor (fraction of rated output).
    Initialized from the static Sprsun R290 estimate; refined when the
    measured electrical draw proves the compressor ran at full capacity."""

    capacity_frac_minus7: float = 0.78
    """Learned capacity fraction at −7 °C outdoor.  Same as above."""

    capacity_minus15_samples: int = 0
    """Measured-full-load observations that updated the −15 °C anchor."""

    capacity_minus7_samples: int = 0
    """Measured-full-load observations that updated the −7 °C anchor."""

    def to_dict(self) -> dict:
        """Serialise to a JSON-safe dict for HA storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CopLearnerState":
        """Deserialise from a stored dict, ignoring unknown keys."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class LearningResult:
    """
    Structured outcome of processing one :class:`CopObservation`.

    Returned by :py:meth:`CopLearner.observe` so the coordinator can log
    what happened and surface diagnostics as sensor attributes.
    """

    accepted: bool
    """False when the observation was rejected by a quality guard."""

    rejection_reason: Optional[str]
    """Human-readable reason for rejection, or None when accepted."""

    cop_measured: Optional[float]
    """Measured COP = heat_out / elec (None when rejected early)."""

    cop_carnot: Optional[float]
    """Theoretical Carnot COP at these conditions."""

    eta_measured: Optional[float]
    """cop_measured / cop_carnot — the per-cycle efficiency ratio."""

    is_defrost_condition: bool
    """True when T_outdoor and RH suggest active icing."""

    eta_carnot_updated: bool
    """True when η_Carnot was updated by this observation."""

    f_defrost_updated: bool
    """True when f_defrost was updated by this observation."""

    eta_carnot_after: float
    """η_Carnot value after processing (useful for trending)."""

    f_defrost_after: float
    """f_defrost value after processing."""

    capacity_updated: bool = False
    """True when a capacity anchor was updated by this observation."""

    capacity_anchor_c: Optional[float] = None
    """Which temperature anchor (°C) was updated, or None."""

    capacity_frac_observed: Optional[float] = None
    """Observed capacity fraction = (heat_kw / rated_kw) at the anchor."""


# ---------------------------------------------------------------------------
# Learner
# ---------------------------------------------------------------------------


class CopLearner:
    """
    Maintains and updates a Carnot-based COP model from real measurements.

    Usage::

        state = CopLearnerState()          # cold start
        learner = CopLearner(state)

        # After each completed run cycle:
        obs = CopObservation(t_outdoor=2.0, rh=85.0, lwt=40.0,
                             heat_out_kwh=3.2, elec_kwh=0.94)
        result = learner.observe(obs)

        # Predict COP for the MPC solver:
        cop = learner.predict_cop(t_outdoor=5.0, rh=60.0, lwt=38.0)

    Parameters
    ----------
    state:
        Mutable state object.  Modified in-place by :py:meth:`observe`.
    eta_learning_rate:
        EMA weight for η_Carnot updates (0 < α ≤ 1).  Lower = slower
        but more noise-resistant.
    defrost_learning_rate:
        EMA weight for f_defrost updates.
    """

    def __init__(
        self,
        state: CopLearnerState,
        eta_learning_rate: float = DEFAULT_ETA_LEARNING_RATE,
        defrost_learning_rate: float = DEFAULT_DEFROST_LEARNING_RATE,
    ) -> None:
        self.state = state
        self._eta_lr = eta_learning_rate
        self._defrost_lr = defrost_learning_rate

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_reliable(self) -> bool:
        """
        True when enough clean-condition samples have been collected that
        the learned η_Carnot is preferable to the EN 14511 cold-start default.
        """
        return self.state.eta_carnot_samples >= MIN_RELIABLE_SAMPLES

    def is_capacity_reliable_at(self, anchor_c: float) -> bool:
        """
        True when the capacity fraction for *anchor_c* has been updated
        by enough max-frequency observations to be trusted over the static default.

        Parameters
        ----------
        anchor_c:
            Temperature anchor in °C.  Supported values: -15.0, -7.0.
        """
        if anchor_c == -15.0:
            return self.state.capacity_minus15_samples >= MIN_RELIABLE_CAPACITY_SAMPLES
        if anchor_c == -7.0:
            return self.state.capacity_minus7_samples >= MIN_RELIABLE_CAPACITY_SAMPLES
        return False

    def get_capacity_anchors(self) -> list[tuple[float, float, bool]]:
        """
        Return learned capacity anchor points as ``(t_outdoor_C, fraction, reliable)``
        triples, including the fixed 7 °C rating point.

        The coordinator uses the ``reliable`` flag to decide whether to replace
        the corresponding static default in ``HeatPumpModel._capacity_temp_curve``
        or keep the hardcoded estimate.

        Returns
        -------
        list of (t_outdoor_C, fraction, reliable)
        """
        return [
            (-15.0, self.state.capacity_frac_minus15,
             self.is_capacity_reliable_at(-15.0)),
            (-7.0,  self.state.capacity_frac_minus7,
             self.is_capacity_reliable_at(-7.0)),
            (7.0,   1.0, True),   # always reliable — definition of the rating point
        ]

    def predict_cop(self, t_outdoor: float, rh: float, lwt: float) -> float:
        """
        Predict effective COP using the current learned parameters.

        Formula::

            COP = η_Carnot × COP_Carnot(T_outdoor, LWT) × f_defrost_factor

        Falls back gracefully when inputs would produce an invalid Carnot COP
        (e.g. LWT ≤ T_outdoor).

        Parameters
        ----------
        t_outdoor:
            Outdoor temperature (°C).
        rh:
            Relative humidity (%).
        lwt:
            Leaving water temperature setpoint (°C).

        Returns
        -------
        float
            Predicted effective COP.  Always ≥ 1.0.
        """
        cop_carnot = _carnot_cop(t_outdoor, lwt)
        if cop_carnot is None:
            # Lift too small — return a safe minimum
            return _COP_MIN

        f = self.state.f_defrost if self._is_defrost_condition(t_outdoor, rh) else 1.0
        return max(_COP_MIN, self.state.eta_carnot * cop_carnot * f)

    def observe(self, obs: CopObservation) -> LearningResult:
        """
        Process one run-cycle observation and update learned parameters.

        The observation is routed to either the η_Carnot track (clean
        conditions) or the f_defrost track (suspected icing), never both,
        to keep the two estimates independent.

        Parameters
        ----------
        obs:
            Completed run-cycle measurement.

        Returns
        -------
        LearningResult
            Full audit trail of what was computed and updated.
        """
        # --- Always update frequency tracking (even if COP observation is rejected) ---
        cap_updated, cap_anchor, cap_frac = self._maybe_update_capacity(obs)

        # --- Quality guards ---
        rejection = self._validate(obs)
        if rejection:
            return LearningResult(
                accepted=False,
                rejection_reason=rejection,
                cop_measured=None,
                cop_carnot=None,
                eta_measured=None,
                is_defrost_condition=self._is_defrost_condition(obs.t_outdoor, obs.rh),
                eta_carnot_updated=False,
                f_defrost_updated=False,
                eta_carnot_after=self.state.eta_carnot,
                f_defrost_after=self.state.f_defrost,
                capacity_updated=cap_updated,
                capacity_anchor_c=cap_anchor,
                capacity_frac_observed=cap_frac,
            )

        # --- Derived quantities ---
        cop_measured = obs.heat_out_kwh / obs.elec_kwh
        cop_carnot = _carnot_cop(obs.t_outdoor, obs.lwt)  # not None after validation

        if cop_measured < _COP_MIN or cop_measured > _COP_MAX:
            return LearningResult(
                accepted=False,
                rejection_reason=f"COP_measured={cop_measured:.2f} outside [{_COP_MIN}, {_COP_MAX}]",
                cop_measured=cop_measured,
                cop_carnot=cop_carnot,
                eta_measured=None,
                is_defrost_condition=self._is_defrost_condition(obs.t_outdoor, obs.rh),
                eta_carnot_updated=False,
                f_defrost_updated=False,
                eta_carnot_after=self.state.eta_carnot,
                f_defrost_after=self.state.f_defrost,
                capacity_updated=cap_updated,
                capacity_anchor_c=cap_anchor,
                capacity_frac_observed=cap_frac,
            )

        eta_measured = cop_measured / cop_carnot
        is_defrost = self._is_defrost_condition(obs.t_outdoor, obs.rh)

        eta_updated = False
        defrost_updated = False

        if not is_defrost:
            # --- Track A: update η_Carnot ---
            # η_measured should be close to η_Carnot.  Clamp to a plausible
            # range before applying EMA so a single bad reading can't
            # derail the model.
            eta_clamped = max(0.25, min(0.65, eta_measured))
            self.state.eta_carnot = _ema(
                self.state.eta_carnot, eta_clamped, self._eta_lr
            )
            self.state.eta_carnot_samples += 1
            eta_updated = True
        else:
            # --- Track B: update f_defrost ---
            # f = how much of the clean baseline is retained during icing.
            # We compare to the *current* learned η_Carnot, not the raw
            # Carnot limit, so the two tracks stay independent.
            f_measured = eta_measured / self.state.eta_carnot
            f_clamped = max(0.5, min(1.0, f_measured))
            self.state.f_defrost = _ema(
                self.state.f_defrost, f_clamped, self._defrost_lr
            )
            self.state.f_defrost_samples += 1
            defrost_updated = True

        return LearningResult(
            accepted=True,
            rejection_reason=None,
            cop_measured=round(cop_measured, 3),
            cop_carnot=round(cop_carnot, 3),
            eta_measured=round(eta_measured, 4),
            is_defrost_condition=is_defrost,
            eta_carnot_updated=eta_updated,
            f_defrost_updated=defrost_updated,
            eta_carnot_after=round(self.state.eta_carnot, 4),
            f_defrost_after=round(self.state.f_defrost, 4),
            capacity_updated=cap_updated,
            capacity_anchor_c=cap_anchor,
            capacity_frac_observed=cap_frac,
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostics(self) -> dict:
        """
        Return a snapshot of the current learned state plus derived values
        suitable for exposure as a Home Assistant sensor attribute.
        """
        return {
            "eta_carnot": round(self.state.eta_carnot, 4),
            "f_defrost": round(self.state.f_defrost, 4),
            "defrost_t_threshold_c": self.state.defrost_t_threshold,
            "defrost_rh_threshold_pct": self.state.defrost_rh_threshold,
            "eta_carnot_samples": self.state.eta_carnot_samples,
            "f_defrost_samples": self.state.f_defrost_samples,
            "is_reliable": self.is_reliable,
            # Capacity learning state
            "capacity_frac_minus15": round(self.state.capacity_frac_minus15, 3),
            "capacity_frac_minus7": round(self.state.capacity_frac_minus7, 3),
            "capacity_minus15_samples": self.state.capacity_minus15_samples,
            "capacity_minus7_samples": self.state.capacity_minus7_samples,
            "capacity_minus15_reliable": self.is_capacity_reliable_at(-15.0),
            "capacity_minus7_reliable": self.is_capacity_reliable_at(-7.0),
            # Spot-check COP prediction at representative operating points
            "cop_predicted_a7w35": round(
                self.predict_cop(t_outdoor=7.0, rh=60.0, lwt=35.0), 2
            ),
            "cop_predicted_a2w35": round(
                self.predict_cop(t_outdoor=2.0, rh=85.0, lwt=35.0), 2
            ),
            "cop_predicted_am7w35": round(
                self.predict_cop(t_outdoor=-7.0, rh=50.0, lwt=35.0), 2
            ),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _maybe_update_capacity(
        self,
        obs: CopObservation,
    ) -> tuple[bool, Optional[float], Optional[float]]:
        """
        Update the capacity fraction for the nearest temperature anchor when
        the observation's measured electrical draw proves the compressor was
        running at full capacity.

        Gate: ``actual_elec_kw > rated_max_elec_kw * 0.9``.  This is
        independent of who commanded the load (MPC, thermostat, legionella
        cycle).  An additional tank-headroom filter prevents updating when
        the tank — not the pump — was the limiting factor.

        Returns
        -------
        (updated, anchor_c, frac_observed)
            *updated*: True when a capacity anchor EMA was updated.
            *anchor_c*: The anchor temperature that was updated (°C), or None.
            *frac_observed*: The observed capacity fraction, or None.
        """
        # Measured-electrical-load gate: only learn capacity when the
        # compressor was drawing ≥ 90 % of its rated max electrical power.
        if not obs.rated_max_elec_kw or obs.rated_max_elec_kw <= 0.0:
            return False, None, None
        if obs.duration_hours <= 0.0 or obs.elec_kwh <= 0.0:
            return False, None, None
        actual_elec_kw = obs.elec_kwh / obs.duration_hours
        if actual_elec_kw < obs.rated_max_elec_kw * 0.9:
            return False, None, None

        # Tank-headroom filter: if the tank could not absorb more heat than
        # was delivered, the tank (not the pump) was the bottleneck.
        if obs.tank_headroom_kwh is not None:
            if obs.heat_out_kwh >= obs.tank_headroom_kwh * 0.9:
                return False, None, None

        if not obs.rated_kw or obs.rated_kw <= 0.0:
            return False, None, None

        actual_kw = obs.heat_out_kwh / obs.duration_hours

        # Observed fraction must be physically plausible
        frac_observed = actual_kw / obs.rated_kw
        if frac_observed <= 0.05 or frac_observed > 1.2:
            return False, None, None

        # Find the nearest temperature anchor within the bin radius
        anchor = _nearest_capacity_anchor(obs.t_outdoor)
        if anchor is None:
            return False, None, None

        # EMA update for the relevant anchor
        frac_clamped = max(0.2, min(1.0, frac_observed))
        if anchor == -15.0:
            self.state.capacity_frac_minus15 = _ema(
                self.state.capacity_frac_minus15, frac_clamped, DEFAULT_CAPACITY_LEARNING_RATE
            )
            self.state.capacity_minus15_samples += 1
        elif anchor == -7.0:
            self.state.capacity_frac_minus7 = _ema(
                self.state.capacity_frac_minus7, frac_clamped, DEFAULT_CAPACITY_LEARNING_RATE
            )
            self.state.capacity_minus7_samples += 1

        return True, anchor, round(frac_observed, 3)

    def _is_defrost_condition(self, t_outdoor: float, rh: float) -> bool:
        """True when outdoor conditions suggest active evaporator icing."""
        return (
            t_outdoor < self.state.defrost_t_threshold
            and rh > self.state.defrost_rh_threshold
        )

    def _validate(self, obs: CopObservation) -> Optional[str]:
        """
        Return a rejection reason string, or None if the observation is usable.

        Checks are ordered cheapest-first.
        """
        if obs.elec_kwh <= 0:
            return "elec_kwh must be positive"
        if obs.heat_out_kwh < _MIN_HEAT_KWH:
            return f"heat_out_kwh={obs.heat_out_kwh:.3f} below minimum {_MIN_HEAT_KWH}"
        if obs.duration_hours < _MIN_DURATION_HOURS:
            return f"duration={obs.duration_hours:.2f}h below minimum {_MIN_DURATION_HOURS}h"
        lift = obs.lwt - obs.t_outdoor
        if lift < _LIFT_MIN_K:
            return f"lift={lift:.1f} K below minimum {_LIFT_MIN_K} K"
        if _carnot_cop(obs.t_outdoor, obs.lwt) is None:
            return "invalid temperatures for Carnot calculation"
        return None


# ---------------------------------------------------------------------------
# Module-level pure functions
# ---------------------------------------------------------------------------


def _carnot_cop(t_outdoor: float, lwt: float) -> Optional[float]:
    """
    Theoretical Carnot COP for a heat pump.

    Returns None when the lift is too small to produce a physically
    meaningful value (avoids division by near-zero).

    Parameters
    ----------
    t_outdoor:
        Outdoor (cold reservoir) temperature (°C).
    lwt:
        Leaving water temperature (hot reservoir) (°C).
    """
    t_hot_k = lwt + _KELVIN_OFFSET
    t_cold_k = t_outdoor + _KELVIN_OFFSET
    lift_k = t_hot_k - t_cold_k

    if lift_k < _LIFT_MIN_K:
        return None

    return t_hot_k / lift_k


def _nearest_capacity_anchor(t_outdoor: float) -> Optional[float]:
    """
    Return the capacity anchor (°C) nearest to *t_outdoor*, or None if the
    observation is too far from any anchor to be useful.

    Only anchors in ``_CAPACITY_ANCHORS`` are candidates.  Observations
    near the 7 °C EN 14511 rating point (fraction ≈ 1.0 by definition)
    are excluded so that mild-weather runtime does not distort the cold-weather
    capacity estimates.

    Parameters
    ----------
    t_outdoor:
        Outdoor temperature of the observation (°C).

    Returns
    -------
    float or None
        Nearest anchor temperature, or None when outside all bin radii.
    """
    best_anchor: Optional[float] = None
    best_dist = float("inf")

    for anchor in _CAPACITY_ANCHORS:
        dist = abs(t_outdoor - anchor)
        if dist < best_dist and dist <= _CAPACITY_BIN_RADIUS:
            best_dist = dist
            best_anchor = anchor

    return best_anchor


def _ema(current: float, new_value: float, alpha: float) -> float:
    """
    Exponential Moving Average update.

        result = current × (1 − α) + new_value × α

    A small α produces slow, noise-resistant convergence.
    A large α reacts quickly but amplifies measurement noise.
    """
    return current * (1.0 - alpha) + new_value * alpha
