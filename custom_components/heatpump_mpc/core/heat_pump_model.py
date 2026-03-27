"""
Core mathematical model for a heat pump.

This module provides a pure Python implementation of the heat pump's
performance model, decoupled from Home Assistant. It calculates the
estimated Coefficient of Performance (COP) based on thermal lift and
environmental factors.
"""

class HeatPumpModel:
    """
    Mathematical model for estimating heat pump performance.

    This class is configured for a typical Sprsun R290 heat pump profile
    by default. It estimates the Coefficient of Performance (COP) based
    on thermal lift and applies a defrost penalty under specific weather conditions.
    """

    def __init__(self) -> None:
        """Initialize the HeatPumpModel."""
        # Sprsun R290 EN 14511 rated (full-load) COP data points.
        # Source: Sprsun R290 product datasheet (220-240 V / 1-phase variant).
        # Represented as (lift_K, cop) where lift = LWT - T_outdoor.
        #
        #   A7/W35:  lift = 35 − 7  = 28 K  →  COP 4.42  (rated)
        #   A7/W45:  lift = 45 − 7  = 38 K  →  COP 3.64  (rated)
        #   A-7/W35: lift = 35 − (-7) = 42 K → COP ~3.33 (extrapolated;
        #            no factory test at this condition — slope from above two)
        #
        # Note: min-load (part-load) COP at A7/W35 reaches 5.61, giving
        # η_Carnot ≈ 0.51.  These rated values (η ≈ 0.40–0.44) apply when
        # the compressor runs at full output, which is the MPC assumption.
        self._cop_curve = [
            (28.0, 4.42),   # A7/W35  — actual datasheet
            (38.0, 3.64),   # A7/W45  — actual datasheet
            (42.0, 3.33),   # A-7/W35 — extrapolated (slope −0.078 COP/K)
        ]
        # Sort curve by lift just in case
        self._cop_curve.sort(key=lambda x: x[0])

        # Modulation gain: ratio of min-load COP to full-load COP at each lift point.
        # From Sprsun R290 datasheet:
        #   A7/W35: min-load COP 5.61, full-load COP 4.42  → gain = 1.269
        #   A7/W45: min-load COP 4.27, full-load COP 3.64  → gain = 1.173
        # The gain decreases with increasing lift (less benefit from throttling at
        # high lift) and is clamped to ≥ 1.0.
        self._modulation_gain_curve = [
            (28.0, 1.269),   # A7/W35
            (38.0, 1.173),   # A7/W45
        ]
        self._modulation_gain_curve.sort(key=lambda x: x[0])

        # ---------------------------------------------------------------------------
        # Capacity derating curves
        #
        # Inverter heat pumps lose the ability to deliver their rated output as
        # outdoor temperature drops and/or leaving water temperature rises.  The
        # MPC must know the *actual* kWh ceiling for each future hour, otherwise
        # it will schedule hours it cannot physically fulfil.
        #
        # We model capacity as a product of two independent factors:
        #
        #   capacity(T_out, LWT) = rated_kW × f_temp(T_out) × f_lwt(LWT)
        #
        # This separable approximation is consistent with EN 14511 test methodology
        # (one variable at a time) and avoids the need for a full 2-D lookup table.
        #
        # IMPORTANT: the default values below are estimates for a Sprsun R290 unit
        # based on typical R290 derating patterns.  Replace them with the actual
        # values from your datasheet's capacity / heating output table once available.
        # The rated point is always A7/W35 = 1.00 by definition.
        # ---------------------------------------------------------------------------

        # f_temp: fraction of rated capacity as a function of outdoor temperature.
        # Sorted ascending by t_outdoor.  Extrapolates linearly beyond the range
        # but is floor-clamped at 0.0 (pump cannot produce negative heat).
        #
        #   A7/W35    →  1.00  (EN 14511 rating point — definition of 100 %)
        #   A-7/W35   →  0.78  (estimated: ~78 % at −7 °C OAT)
        #   A-15/W35  →  0.63  (estimated: ~63 % at −15 °C OAT)
        #   A-20/W35  →  0.52  (estimated: ~52 % approaching min operating limit)
        #
        # Replace with real datasheet capacity figures once available.
        self._capacity_temp_curve: list[tuple[float, float]] = [
            (-20.0, 0.52),
            (-15.0, 0.63),
            ( -7.0, 0.78),
            (  7.0, 1.00),
        ]

        # f_lwt: capacity fraction relative to W35 as a function of leaving water
        # temperature.  Higher LWT → greater compression ratio → lower capacity.
        #
        #   W35  →  1.00  (reference)
        #   W45  →  0.95  (estimated: ~5 % lower)
        #   W55  →  0.89  (estimated: ~11 % lower)
        #
        # Replace with real datasheet capacity figures once available.
        self._capacity_lwt_curve: list[tuple[float, float]] = [
            (35.0, 1.00),
            (45.0, 0.95),
            (55.0, 0.89),
        ]

    def _calculate_lift(self, t_outdoor: float, lwt: float) -> float:
        """
        Calculate the thermal lift.

        Args:
            t_outdoor (float): The outdoor temperature in °C.
            lwt (float): The leaving water temperature (setpoint) in °C.

        Returns:
            float: The thermal lift (LWT - T_outdoor).
        """
        return lwt - t_outdoor

    def _get_base_cop(self, lift: float) -> float:
        """
        Calculate the baseline COP using linear interpolation on standard test points.

        Args:
            lift (float): The thermal lift in °C or K.

        Returns:
            float: The interpolated base Coefficient of Performance.
        """
        if len(self._cop_curve) >= 2:
            # Extrapolate below lowest lift
            if lift <= self._cop_curve[0][0]:
                lift_1, cop_1 = self._cop_curve[0]
                lift_2, cop_2 = self._cop_curve[1]
                slope = (cop_2 - cop_1) / (lift_2 - lift_1)
                return cop_1 + slope * (lift - lift_1)

            # Extrapolate above highest lift
            if lift >= self._cop_curve[-1][0]:
                lift_n2, cop_n2 = self._cop_curve[-2]
                lift_n1, cop_n1 = self._cop_curve[-1]
                slope = (cop_n1 - cop_n2) / (lift_n1 - lift_n2)
                # Cap the COP at 1.0 (pure resistive heating equivalence)
                return max(1.0, cop_n1 + slope * (lift - lift_n1))

            # Interpolate between points
            for i in range(len(self._cop_curve) - 1):
                lift_low, cop_low = self._cop_curve[i]
                lift_high, cop_high = self._cop_curve[i+1]

                if lift_low <= lift <= lift_high:
                    ratio = (lift - lift_low) / (lift_high - lift_low)
                    return cop_low + ratio * (cop_high - cop_low)

        # Fallback if no conditions matched
        return self._cop_curve[-1][1]

    def _get_defrost_penalty(self, t_outdoor: float, rh: float) -> float:
        """
        Calculate the defrost penalty factor.

        According to DESIGN.md, the penalty is 0.85 when T_out < 7.0°C
        and RH > 70.0%, otherwise 1.0.

        Args:
            t_outdoor (float): The outdoor temperature in °C.
            rh (float): The relative humidity in %.

        Returns:
            float: A multiplier representing the defrost efficiency penalty.
        """
        if t_outdoor < 7.0 and rh > 70.0:
            return 0.85
        return 1.0

    def _get_modulation_gain(self, lift: float) -> float:
        """
        Return the min-load / full-load COP ratio interpolated at the given lift.

        The gain is always ≥ 1.0 (part-load COP is never worse than full-load).

        Args:
            lift (float): Thermal lift in K (LWT − T_outdoor).

        Returns:
            float: Modulation gain factor.
        """
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
        """
        Calculate the effective COP at a specific inverter output level.

        COP is linearly interpolated between the min-load COP (at *min_output_kw*)
        and the full-load COP (at *max_output_kw*) using the modulation gain curve
        derived from datasheet measurements.

        Args:
            t_outdoor (float): Outdoor temperature in °C.
            rh (float): Relative humidity in %.
            lwt (float): Leaving water temperature setpoint in °C.
            output_kw (float): Requested thermal output in kW.
            max_output_kw (float): Rated full-load thermal output in kW.
            min_output_kw (float): Minimum inverter output in kW.

        Returns:
            float: Effective COP at the requested output level.
        """
        cop_full = self.get_effective_cop(t_outdoor, rh, lwt)

        if min_output_kw >= max_output_kw or output_kw >= max_output_kw - 1e-9:
            return cop_full

        lift = self._calculate_lift(t_outdoor, lwt)
        gain = self._get_modulation_gain(lift)
        cop_min_load = cop_full * gain

        # Fraction from min→max load: 0 = min load, 1 = full load
        ratio = (output_kw - min_output_kw) / (max_output_kw - min_output_kw)
        ratio = max(0.0, min(1.0, ratio))
        return cop_min_load + ratio * (cop_full - cop_min_load)

    def apply_learned_capacity(
        self,
        anchors: list[tuple[float, float, bool]],
    ) -> None:
        """
        Update ``_capacity_temp_curve`` with values learned from real measurements.

        Only anchors marked as reliable (third element = True) replace the
        corresponding static estimate.  Unreliable anchors are left unchanged
        so the static prior continues to apply until enough data has accumulated.

        The curve is re-sorted after the update so interpolation remains valid.

        Parameters
        ----------
        anchors:
            List of ``(t_outdoor_C, fraction, reliable)`` triples as returned
            by :py:meth:`~cop_learner.CopLearner.get_capacity_anchors`.
        """
        # Build a working dict from the current curve for easy in-place updates
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
        """
        Interpolate (or extrapolate) a sorted list of (x, y) pairs at *x*.

        Extrapolation uses the slope of the nearest segment.  The result is
        clamped to ``clamp_low`` from below, preventing physically impossible
        negative fractions.

        Args:
            curve: Sorted list of (x_val, y_val) tuples.
            x: Query point.
            clamp_low: Minimum allowed return value (default 0.0).

        Returns:
            float: Interpolated/extrapolated value, ≥ clamp_low.
        """
        if not curve:
            return clamp_low
        if len(curve) == 1:
            return max(clamp_low, curve[0][1])

        # Below lowest point — extrapolate from first segment
        if x <= curve[0][0]:
            x0, y0 = curve[0]
            x1, y1 = curve[1]
            slope = (y1 - y0) / (x1 - x0)
            return max(clamp_low, y0 + slope * (x - x0))

        # Above highest point — extrapolate from last segment
        if x >= curve[-1][0]:
            x0, y0 = curve[-2]
            x1, y1 = curve[-1]
            slope = (y1 - y0) / (x1 - x0)
            return max(clamp_low, y1 + slope * (x - x1))

        # Interpolate between bracketing points
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
        """
        Return the maximum thermal output the heat pump can deliver (kW).

        Capacity is modelled as the product of two independent derating factors:

        .. code-block:: text

            capacity = rated_kW × f_temp(T_outdoor) × f_lwt(LWT)

        where ``f_temp`` captures the compressor's loss of volumetric efficiency
        at low outdoor temperatures and ``f_lwt`` captures the additional
        compression load imposed by a higher leaving-water temperature.

        Both factors are interpolated from their respective curves and clamped
        to [0, 1].  The final result is clamped to [0, rated_kW].

        This value is used by the MPC solver to cap heat delivery per hour,
        preventing the scheduler from assuming the tank can be charged faster
        than the heat pump physically allows at that outdoor condition.

        Args:
            t_outdoor (float): Outdoor temperature in °C.
            lwt (float): Leaving water temperature setpoint in °C.
            rated_kw (float): User-configured rated thermal output (kW).

        Returns:
            float: Maximum deliverable thermal output in kW.
        """
        f_temp = self._interp_1d(self._capacity_temp_curve, t_outdoor, clamp_low=0.0)
        f_lwt  = self._interp_1d(self._capacity_lwt_curve,  lwt,       clamp_low=0.0)

        # Clamp both fractions to [0, 1] so a bad curve can't over-rate the pump
        f_temp = min(1.0, f_temp)
        f_lwt  = min(1.0, f_lwt)

        return rated_kw * f_temp * f_lwt

    def get_effective_cop(self, t_outdoor: float, rh: float, lwt: float) -> float:
        """
        Calculate the effective Coefficient of Performance (COP).

        Combines the base COP curve calculation based on thermal lift
        with environmental penalties (like defrost cycles).

        Args:
            t_outdoor (float): The outdoor temperature in °C.
            rh (float): The relative humidity in %.
            lwt (float): The leaving water temperature (setpoint) in °C.

        Returns:
            float: The effective COP.
        """
        lift = self._calculate_lift(t_outdoor, lwt)
        cop_base = self._get_base_cop(lift)
        f_defrost = self._get_defrost_penalty(t_outdoor, rh)

        return cop_base * f_defrost
