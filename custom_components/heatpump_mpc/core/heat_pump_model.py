"""
Core mathematical model for a heat pump.

This module provides a pure Python implementation of the heat pump's
performance model, decoupled from Home Assistant. It calculates the
estimated Coefficient of Performance (COP) based on thermal lift and
environmental factors.

The model is initialised from a *profile* dict that captures the specific
heat pump type and refrigerant.  Pre-defined profiles are available for
common configurations (ASHP R290, GSHP R290, etc.); the ``get_profile``
helper selects the right one from config-flow choices.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Pre-defined profiles
# ---------------------------------------------------------------------------

_ASHP_R290: dict[str, Any] = {
    "description": "Air-source heat pump, R290 (propane)",
    # EN 14511 rated (full-load) COP data points: (lift_K, COP).
    # Source: Sprsun R290 product datasheet (220-240 V / 1-phase).
    "cop_curve": [
        (28.0, 4.42),   # A7/W35
        (38.0, 3.64),   # A7/W45
        (42.0, 3.33),   # A-7/W35 (extrapolated)
    ],
    # Modulation gain: min-load COP / full-load COP at each lift point.
    "modulation_gain_curve": [
        (28.0, 1.269),   # A7/W35: 5.61 / 4.42
        (38.0, 1.173),   # A7/W45: 4.27 / 3.64
    ],
    # Capacity fraction vs outdoor/source temperature.
    "capacity_temp_curve": [
        (-20.0, 0.52),
        (-15.0, 0.63),
        ( -7.0, 0.78),
        (  7.0, 1.00),
    ],
    # Capacity fraction vs leaving water temperature.
    "capacity_lwt_curve": [
        (35.0, 1.00),
        (45.0, 0.95),
        (55.0, 0.89),
    ],
    "eta_carnot_default": 0.42,
    "f_defrost_default": 0.85,
    "has_defrost": True,
}

_ASHP_R32: dict[str, Any] = {
    "description": "Air-source heat pump, R32",
    # R32 typically has slightly lower η_Carnot than R290 at equivalent
    # conditions.  COP curve shape is similar but shifted down.
    "cop_curve": [
        (28.0, 4.10),   # A7/W35 (typical R32 datasheet)
        (38.0, 3.35),   # A7/W45
        (42.0, 3.05),   # A-7/W35 (extrapolated)
    ],
    "modulation_gain_curve": [
        (28.0, 1.25),
        (38.0, 1.16),
    ],
    "capacity_temp_curve": [
        (-20.0, 0.48),
        (-15.0, 0.58),
        ( -7.0, 0.75),
        (  7.0, 1.00),
    ],
    "capacity_lwt_curve": [
        (35.0, 1.00),
        (45.0, 0.93),
        (55.0, 0.86),
    ],
    "eta_carnot_default": 0.39,
    "f_defrost_default": 0.85,
    "has_defrost": True,
}

_GSHP_R290: dict[str, Any] = {
    "description": "Ground-source heat pump, R290 (propane)",
    # GSHP operates at lower lift (brine 0–8 °C vs air -20–15 °C).
    # Higher COP at equivalent lift due to stable source temperature.
    "cop_curve": [
        (27.0, 4.80),   # B0/W27 (floor heating, typical)
        (30.0, 4.40),   # B0/W30
        (35.0, 3.90),   # B0/W35
        (45.0, 3.20),   # B0/W45 (radiators)
    ],
    "modulation_gain_curve": [
        (27.0, 1.30),
        (35.0, 1.22),
        (45.0, 1.15),
    ],
    # GSHP capacity vs brine temperature — relatively flat.
    # Use 1.0 across the expected range; CopLearner refines from data.
    "capacity_temp_curve": [
        (-5.0, 0.90),
        ( 0.0, 0.95),
        ( 5.0, 1.00),
        (10.0, 1.02),
    ],
    "capacity_lwt_curve": [
        (35.0, 1.00),
        (45.0, 0.93),
        (55.0, 0.85),
    ],
    "eta_carnot_default": 0.45,
    "f_defrost_default": 1.0,   # no defrost for GSHP
    "has_defrost": False,
}

_GSHP_R32: dict[str, Any] = {
    "description": "Ground-source heat pump, R32",
    "cop_curve": [
        (27.0, 4.50),
        (30.0, 4.10),
        (35.0, 3.60),
        (45.0, 2.95),
    ],
    "modulation_gain_curve": [
        (27.0, 1.28),
        (35.0, 1.20),
        (45.0, 1.13),
    ],
    "capacity_temp_curve": [
        (-5.0, 0.88),
        ( 0.0, 0.93),
        ( 5.0, 1.00),
        (10.0, 1.02),
    ],
    "capacity_lwt_curve": [
        (35.0, 1.00),
        (45.0, 0.91),
        (55.0, 0.83),
    ],
    "eta_carnot_default": 0.42,
    "f_defrost_default": 1.0,
    "has_defrost": False,
}

# Registry: (hp_type, refrigerant) → profile dict.
# "other" refrigerant maps to R290 priors (safest default).
PROFILES: dict[tuple[str, str], dict[str, Any]] = {
    ("ashp", "r290"):   _ASHP_R290,
    ("ashp", "r32"):    _ASHP_R32,
    ("ashp", "r410a"):  _ASHP_R32,    # R410A is close enough to R32 for priors
    ("ashp", "other"):  _ASHP_R290,   # conservative fallback
    ("gshp", "r290"):   _GSHP_R290,
    ("gshp", "r32"):    _GSHP_R32,
    ("gshp", "r410a"):  _GSHP_R32,
    ("gshp", "other"):  _GSHP_R290,
    ("a2a",  "r290"):   _ASHP_R290,   # A2A uses ASHP physics for now
    ("a2a",  "r32"):    _ASHP_R32,
    ("a2a",  "r410a"):  _ASHP_R32,
    ("a2a",  "other"):  _ASHP_R290,
}

DEFAULT_PROFILE = _ASHP_R290


def get_profile(hp_type: str = "ashp", refrigerant: str = "r290") -> dict[str, Any]:
    """Look up the cold-start profile for a given HP type and refrigerant.

    Falls back to ASHP R290 if the combination is unknown.
    """
    return PROFILES.get((hp_type.lower(), refrigerant.lower()), DEFAULT_PROFILE)


class HeatPumpModel:
    """
    Mathematical model for estimating heat pump performance.

    Initialised from a profile dict that sets cold-start priors for COP
    curve, modulation gain, capacity derating, and defrost behaviour.
    The CopLearner refines these from real measurements at runtime.

    Parameters
    ----------
    profile:
        A profile dict (from ``get_profile`` or ``PROFILES``).
        When omitted, uses ASHP R290 defaults (backward-compatible).
    """

    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        if profile is None:
            profile = DEFAULT_PROFILE

        self._cop_curve: list[tuple[float, float]] = sorted(
            profile["cop_curve"], key=lambda x: x[0]
        )
        self._modulation_gain_curve: list[tuple[float, float]] = sorted(
            profile["modulation_gain_curve"], key=lambda x: x[0]
        )
        self._capacity_temp_curve: list[tuple[float, float]] = sorted(
            profile["capacity_temp_curve"], key=lambda x: x[0]
        )
        self._capacity_lwt_curve: list[tuple[float, float]] = sorted(
            profile["capacity_lwt_curve"], key=lambda x: x[0]
        )
        self.has_defrost: bool = profile.get("has_defrost", True)
        self._f_defrost_prior: float = profile.get("f_defrost_default", 0.85)

    # ------------------------------------------------------------------
    # Core calculations (unchanged API)
    # ------------------------------------------------------------------

    def _calculate_lift(self, t_outdoor: float, lwt: float) -> float:
        """Calculate the thermal lift (LWT - T_source)."""
        return lwt - t_outdoor

    def _get_base_cop(self, lift: float) -> float:
        """Baseline COP via linear interpolation on rated test points."""
        if len(self._cop_curve) >= 2:
            if lift <= self._cop_curve[0][0]:
                lift_1, cop_1 = self._cop_curve[0]
                lift_2, cop_2 = self._cop_curve[1]
                slope = (cop_2 - cop_1) / (lift_2 - lift_1)
                return cop_1 + slope * (lift - lift_1)

            if lift >= self._cop_curve[-1][0]:
                lift_n2, cop_n2 = self._cop_curve[-2]
                lift_n1, cop_n1 = self._cop_curve[-1]
                slope = (cop_n1 - cop_n2) / (lift_n1 - lift_n2)
                return max(1.0, cop_n1 + slope * (lift - lift_n1))

            for i in range(len(self._cop_curve) - 1):
                lift_low, cop_low = self._cop_curve[i]
                lift_high, cop_high = self._cop_curve[i+1]
                if lift_low <= lift <= lift_high:
                    ratio = (lift - lift_low) / (lift_high - lift_low)
                    return cop_low + ratio * (cop_high - cop_low)

        return self._cop_curve[-1][1]

    def _get_defrost_penalty(self, t_outdoor: float, rh: float) -> float:
        """Defrost penalty factor: < 1.0 when icing is expected, else 1.0.

        Disabled entirely for GSHP (no evaporator icing in brine circuits).
        """
        if not self.has_defrost:
            return 1.0
        if t_outdoor < 7.0 and rh > 70.0:
            return self._f_defrost_prior
        return 1.0

    def _get_modulation_gain(self, lift: float) -> float:
        """Min-load / full-load COP ratio, interpolated at the given lift."""
        curve = self._modulation_gain_curve
        if not curve:
            return 1.0
        if len(curve) == 1:
            return curve[0][1]

        if lift <= curve[0][0]:
            l1, g1 = curve[0]
            l2, g2 = curve[1]
            slope = (g2 - g1) / (l2 - l1)
            return max(1.0, g1 + slope * (lift - l1))

        if lift >= curve[-1][0]:
            l1, g1 = curve[-2]
            l2, g2 = curve[-1]
            slope = (g2 - g1) / (l2 - l1)
            return max(1.0, g2 + slope * (lift - l2))

        for i in range(len(curve) - 1):
            l1, g1 = curve[i]
            l2, g2 = curve[i + 1]
            if l1 <= lift <= l2:
                ratio = (lift - l1) / (l2 - l1)
                return g1 + ratio * (g2 - g1)

        return curve[-1][1]

    def get_cop_at_output(
        self,
        t_outdoor: float,
        rh: float,
        lwt: float,
        output_kw: float,
        max_output_kw: float,
        min_output_kw: float,
    ) -> float:
        """Effective COP at a specific inverter output level."""
        cop_full = self.get_effective_cop(t_outdoor, rh, lwt)

        if min_output_kw >= max_output_kw or output_kw >= max_output_kw - 1e-9:
            return cop_full

        lift = self._calculate_lift(t_outdoor, lwt)
        gain = self._get_modulation_gain(lift)
        cop_min_load = cop_full * gain

        ratio = (output_kw - min_output_kw) / (max_output_kw - min_output_kw)
        ratio = max(0.0, min(1.0, ratio))
        return cop_min_load + ratio * (cop_full - cop_min_load)

    def apply_learned_capacity(
        self,
        anchors: list[tuple[float, float, bool]],
    ) -> None:
        """Update ``_capacity_temp_curve`` with learned values.

        Only reliable anchors replace the static estimate.
        """
        curve_dict: dict[float, float] = {t: f for t, f in self._capacity_temp_curve}
        for t_anchor, frac, reliable in anchors:
            if reliable:
                curve_dict[t_anchor] = frac
        self._capacity_temp_curve = sorted(curve_dict.items(), key=lambda x: x[0])

    @staticmethod
    def _interp_1d(
        curve: list[tuple[float, float]],
        x: float,
        clamp_low: float = 0.0,
    ) -> float:
        """Interpolate/extrapolate a sorted (x, y) curve at *x*."""
        if not curve:
            return clamp_low
        if len(curve) == 1:
            return max(clamp_low, curve[0][1])

        if x <= curve[0][0]:
            x0, y0 = curve[0]
            x1, y1 = curve[1]
            slope = (y1 - y0) / (x1 - x0)
            return max(clamp_low, y0 + slope * (x - x0))

        if x >= curve[-1][0]:
            x0, y0 = curve[-2]
            x1, y1 = curve[-1]
            slope = (y1 - y0) / (x1 - x0)
            return max(clamp_low, y1 + slope * (x - x1))

        for i in range(len(curve) - 1):
            x0, y0 = curve[i]
            x1, y1 = curve[i + 1]
            if x0 <= x <= x1:
                ratio = (x - x0) / (x1 - x0)
                return max(clamp_low, y0 + ratio * (y1 - y0))

        return max(clamp_low, curve[-1][1])

    def get_max_output_at(
        self,
        t_outdoor: float,
        lwt: float,
        rated_kw: float,
    ) -> float:
        """Maximum thermal output at given conditions (kW).

        ``capacity = rated_kW × f_temp(T_source) × f_lwt(LWT)``
        """
        f_temp = self._interp_1d(self._capacity_temp_curve, t_outdoor, clamp_low=0.0)
        f_lwt  = self._interp_1d(self._capacity_lwt_curve,  lwt,       clamp_low=0.0)

        f_temp = min(1.0, f_temp)
        f_lwt  = min(1.0, f_lwt)

        return rated_kw * f_temp * f_lwt

    def get_effective_cop(self, t_outdoor: float, rh: float, lwt: float) -> float:
        """Effective COP combining base curve and defrost penalty."""
        lift = self._calculate_lift(t_outdoor, lwt)
        cop_base = self._get_base_cop(lift)
        f_defrost = self._get_defrost_penalty(t_outdoor, rh)

        return cop_base * f_defrost
