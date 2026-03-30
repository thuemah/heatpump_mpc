"""
MPC Solver for heat pump scheduling optimisation.

Pure Python. No Home Assistant dependencies.

Implements a greedy three-mass Model Predictive Control algorithm:
  - Mass 1 (Building): per-hour heat demand supplied via a shunt valve.
  - Mass 2 (buffer tank): fast thermal battery charged in SH mode.
  - Mass 3 (DHW tank): domestic hot water tank, charged in DHW mode.

DHW and space-heating (SH) modes are mutually exclusive: the heat pump
runs in exactly one mode per hour.  The solver schedules DHW first (fixed
LWT, greedy Phase 1 + 2), marks those hours as blocked, then runs the
existing SH optimisation on remaining hours.

When ``config.dhw_enabled`` is False the DHW pre-pass is skipped entirely
and the solver behaves identically to the original two-mass algorithm.

Per-hour output selection (SH)
-------------------------------
For each LWT candidate the solver:

  Phase 1 — Constraint satisfaction
    Uses full rated output (``heat_pump_output_kw``) to schedule the
    minimum set of hours that keep the tank above empty.  High output
    fills the tank quickly, which minimises the number of pump-on hours
    needed to satisfy hard constraints.

  Post-Phase-1 downgrade
    Each Phase-1 pump-on hour is tentatively downgraded to
    ``min_output_kw`` (which delivers heat at a better COP due to
    inverter modulation gain).  The downgrade is kept if it is still
    feasible; otherwise reverted to full output.

  Phase 2 — Opportunistic pre-charging
    Remaining off-hours are considered for pre-charging, cheapest COP
    first, using ``min_output_kw``.  An hour is added only if the tank
    has actual headroom at that point in the simulation (``heat_in > 0``).
    Because Phase 1 typically fills the tank quickly using full output,
    the tank is often near capacity in Phase 2, causing most candidate
    hours to be rejected — producing the natural on/off pattern that
    correctly concentrates run-time in high-COP windows.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

from dataclasses import dataclass, field, replace as _dc_replace

from .heat_pump_model import HeatPumpModel

# Water heat capacity: 1.16 Wh per litre per Kelvin → 0.00116 kWh/L/K
_WATER_KWH_PER_LITRE_K: float = 1.16e-3


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class HorizonPoint:
    """Input data for a single one-hour slot in the optimisation horizon."""

    price: float
    """Electricity price for this hour (currency / kWh)."""

    t_outdoor: float
    """Outdoor temperature (°C)."""

    rh: float
    """Relative humidity (%)."""

    house_demand: float
    """Thermal energy the building needs this hour to stay in the comfort band (kWh_th).

    Derived by the coordinator: Heating Analytics provides electrical kWh; the
    coordinator converts to thermal kWh by multiplying by the COP estimated at
    the heating-curve-prescribed LWT for that outdoor temperature.  All solver
    and tank-simulation maths use this thermal value — electricity re-enters
    only through ``cost = thermal_kWh / COP × price``."""

    dhw_demand: float = 0.0
    """Thermal energy drawn from the DHW tank this hour (kWh_th).
    Derived by the coordinator: daily DHW demand ÷ 24.
    Zero when DHW scheduling is disabled."""


@dataclass
class MpcConfig:
    """Configuration parameters for the MPC solver."""

    min_lwt: float
    """Absolute minimum leaving water temperature (°C). Never violated."""

    max_lwt: float
    """Maximum leaving water temperature the heat pump may target (°C)."""

    max_tank_temp: float
    """Safety ceiling for the buffer tank (°C). Charging stops when reached."""

    heat_pump_output_kw: float
    """Nominal thermal output of the heat pump (kW).
    Over a 1-hour time step this equals the kWh delivered per run hour."""

    tank_volume_liters: float = 300.0
    """Volume of the buffer tank (litres). Default: 300 L."""

    lwt_step: float = 5.0
    """Increment between consecutive LWT candidates (°C). Default: 5 °C."""

    tank_standby_loss_kwh: float = 0.05
    """Hourly standing heat loss from the insulated tank (kWh). Default: 0.05 kWh."""

    min_output_kw: float = 4.37
    """Minimum inverter output (kW). Used as the output level for pre-charging
    (Phase 2) and as the downgrade target after Phase 1. Delivers heat at a
    better COP than full output due to inverter modulation gain.
    Default: 4.37 kW (Sprsun R290 datasheet minimum)."""

    k_emission: float = 0.0
    """Thermal transfer coefficient of the building's emission system (kW per K
    above t_room).  Heat transferred to the building each hour:
        Q_thermal = k_emission × (LWT − t_room)     [kWh_th / h]
    When zero (default) the emission constraint is disabled — no LWT filtering.
    Computed by the coordinator from the heating curve config and Heating
    Analytics demand data; purely thermal, independent of COP or electricity."""

    t_room: float = 21.0
    """Indoor comfort temperature (°C).  Used together with k_emission to
    compute the minimum LWT required to cover the building's hourly thermal
    demand via the emission system."""

    start_penalty_kwh: float = 0.0
    """Electrical energy (kWh_el) added to scenario cost for each compressor
    start event (transition from off → on).  Penalises fragmented schedules
    with many short cycles and favours fewer, longer run periods.
    Default: 0.0 (disabled).  A value of ~0.2 kWh represents roughly
    10 minutes of inefficient startup at ~1.2 kW electrical input."""

    # ------------------------------------------------------------------
    # DHW (Domestic Hot Water) parameters
    # ------------------------------------------------------------------

    dhw_enabled: bool = False
    """When True the solver pre-schedules DHW reheating hours before running
    the SH optimisation.  Default: False (feature disabled)."""

    dhw_tank_volume_liters: float = 180.0
    """DHW tank volume (litres). Default: 180 L."""

    dhw_min_temp: float = 40.0
    """Temperature below which the DHW tank requires reheating (°C).
    Used as the lower bound for DHW tank energy calculations."""

    dhw_target_temp: float = 55.0
    """Target temperature to which the HP heats the DHW tank (°C)."""

    dhw_lwt: float = 55.0
    """Fixed leaving water temperature used when the HP runs in DHW mode (°C).
    Independent of the SH LWT optimisation — DHW always runs at this temperature."""

    dhw_ready_by_hours: list[int] = field(default_factory=list)
    """Horizon slot indices where the DHW tank must be at ≥ 90 % of its maximum
    energy (``dhw_target_temp − dhw_min_temp``).  Computed by the coordinator
    from user-configured HH:MM ready-by times at each solver run.  An empty
    list disables the feature (only the never-empty depletion constraint
    applies)."""


@dataclass
class HourPlan:
    """Scheduled action and simulated state for a single hour."""

    hour_index: int
    """Zero-based index within the horizon."""

    pump_on: bool
    """True if the heat pump is scheduled to run this hour."""

    lwt: float
    """Leaving water temperature setpoint for this hour (°C)."""

    output_kw: float
    """Effective thermal output for this hour (kW).
    May be ``min_output_kw`` or ``heat_pump_output_kw`` depending on which
    the per-hour optimiser selected, clamped to the physical capacity ceiling."""

    max_capacity_kw: float
    """Physical capacity ceiling at this hour's conditions (kW).
    The pump cannot exceed this regardless of the inverter setting."""

    cop_effective: float
    """Estimated effective COP (including defrost penalty) at this hour."""

    cost_per_kwh_heat: float
    """Decision metric: electricity price divided by effective COP."""

    heat_delivered_kwh: float
    """Thermal energy actually delivered to the tank this hour (kWh).
    May be less than the nominal output when the tank approaches max_tank_temp."""

    tank_energy_kwh: float
    """Stored tank energy at the *end* of this hour (kWh above min_lwt).
    Negative values indicate a constraint violation in an infeasible scenario."""

    tank_temp_c: float
    """Estimated tank temperature at end of hour (°C)."""

    electricity_cost: float
    """Electricity cost for this hour (currency), including any start penalty."""

    start_event: bool = False
    """True when this is the first hour of a new run cycle (off→on transition).
    The start penalty (if configured) is charged at this hour."""

    dhw_on: bool = False
    """True when the HP is scheduled to run in DHW mode this hour.
    Mutually exclusive with ``pump_on`` (SH mode)."""

    dhw_heat_delivered_kwh: float = 0.0
    """Thermal energy delivered to the DHW tank this hour (kWh_th).
    Non-zero only when ``dhw_on`` is True."""

    dhw_tank_energy_kwh: float = 0.0
    """Stored DHW tank energy at the end of this hour (kWh above dhw_min_temp)."""

    dhw_tank_temp_c: float = 0.0
    """Estimated DHW tank temperature at end of hour (°C)."""


@dataclass
class MpcResult:
    """Result returned by :py:meth:`MpcSolver.solve`."""

    feasible: bool
    """True when all tank constraints are satisfied throughout the horizon."""

    optimal_lwt: float
    """The LWT setpoint for the chosen scenario (°C)."""

    optimal_output_kw: float
    """The inverter output level chosen for the *current* hour (hour 0) of the
    optimal scenario (kW).  Subsequent hours may use a different output level;
    consult ``schedule[t].output_kw`` for the full per-hour plan."""

    total_cost: float
    """Total electricity cost over the horizon for the chosen scenario (currency)."""

    schedule: list[HourPlan] = field(default_factory=list)
    """Per-hour plan. Index 0 is the current hour."""

    dhw_on_now: bool = False
    """True when DHW mode is scheduled for the current hour (hour 0)."""

    optimal_dhw_setpoint: float = 0.0
    """Recommended DHW tank target temperature to write to the heat pump (°C).
    Equals ``dhw_target_temp`` when ``dhw_on_now`` is True; otherwise
    ``dhw_min_temp - 1`` to block unsolicited reheating.
    Zero when DHW scheduling is disabled."""

    dhw_planned_hours: int = 0
    """Number of DHW-mode hours in the current horizon."""


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


class MpcSolver:
    """
    Greedy two-mass MPC solver for heat pump + buffer tank scheduling.

    Parameters
    ----------
    model:
        A configured :class:`HeatPumpModel` instance used for COP estimation.
    """

    def __init__(self, model: HeatPumpModel) -> None:
        self._model = model

    # ------------------------------------------------------------------
    # DHW pre-scheduler (Phase 1 and Phase 2 are separate methods)
    # ------------------------------------------------------------------

    def _dhw_setup(
        self,
        horizon: list[HorizonPoint],
        dhw_tank_temp_init: float,
        config: MpcConfig,
    ) -> tuple[int, float, float, list[float], list[float]]:
        """Compute shared DHW constants and per-hour metrics.

        Returns ``(n, dhw_energy_init, dhw_max_energy, dhw_cap, dhw_cop)``.
        Called by both :py:meth:`_solve_dhw_phase1` and
        :py:meth:`_apply_dhw_phase2` to avoid duplicating the setup.
        """
        n = len(horizon)
        kwh_per_k = config.dhw_tank_volume_liters * _WATER_KWH_PER_LITRE_K
        dhw_energy_init = max(
            0.0, (dhw_tank_temp_init - config.dhw_min_temp) * kwh_per_k
        )
        dhw_max_energy = (config.dhw_target_temp - config.dhw_min_temp) * kwh_per_k

        dhw_cap: list[float] = []
        dhw_cop: list[float] = []
        for pt in horizon:
            cap = self._model.get_max_output_at(
                pt.t_outdoor, config.dhw_lwt, config.heat_pump_output_kw
            )
            cop = max(
                0.1,
                self._model.get_effective_cop(pt.t_outdoor, pt.rh, config.dhw_lwt),
            )
            dhw_cap.append(cap)
            dhw_cop.append(cop)

        return n, dhw_energy_init, dhw_max_energy, dhw_cap, dhw_cop

    def _solve_dhw_phase1(
        self,
        horizon: list[HorizonPoint],
        dhw_tank_temp_init: float,
        config: MpcConfig,
    ) -> list[bool]:
        """
        Phase 1 — DHW constraint satisfaction (survival scheduling only).

        Schedules the minimum set of hours that keep the DHW tank from going
        empty and satisfy any ready-by constraints throughout the horizon.
        Uses full rated output to fill the tank quickly, minimising the number
        of hours booked.

        Returns a boolean mask of length ``n``; True = HP runs in DHW mode
        that hour.  These hours are blocked for SH Phase 1 + Phase 2 scheduling.

        DHW Phase 2 (opportunistic pre-charging) runs separately, after SH
        scheduling, so that it never steals hours that SH Phase 1 needs.
        """
        n, dhw_energy_init, dhw_max_energy, dhw_cap, dhw_cop = self._dhw_setup(
            horizon, dhw_tank_temp_init, config
        )

        dhw_on = [False] * n

        ready_by_set: set[int] = set(config.dhw_ready_by_hours)
        _READY_THRESHOLD = 0.90
        _ready_min_energy = dhw_max_energy * _READY_THRESHOLD

        def _find_first_violation(schedule: list[bool]) -> int | None:
            """Return the earliest hour index with a constraint violation.

            Two violation types are detected in a single forward pass:

            * **Depletion** — ``tank_end < 0``: the tank runs dry.
            * **Ready-by** — at a user-configured index the tank energy is
              below 90 % of the maximum (``dhw_target_temp − dhw_min_temp``).

            Returns ``None`` when the schedule satisfies all constraints.
            """
            e = dhw_energy_init
            for t in range(n):
                if schedule[t]:
                    headroom = max(0.0, dhw_max_energy - e)
                    e += min(dhw_cap[t], headroom)
                e -= horizon[t].dhw_demand
                if e < 0.0:
                    return t
                if t in ready_by_set and e < _ready_min_energy:
                    return t
                e = max(0.0, e)
            return None

        for _ in range(n):
            violation = _find_first_violation(dhw_on)
            if violation is None:
                break
            candidates = [t for t in range(violation + 1) if not dhw_on[t]]
            if not candidates:
                _LOGGER.warning(
                    "DHW solver: cannot satisfy constraint at hour %d — "
                    "DHW tank will be depleted.",
                    violation,
                )
                break
            latest = candidates[-1]
            dhw_on[latest] = True

        return dhw_on

    def _apply_dhw_phase2(
        self,
        horizon: list[HorizonPoint],
        dhw_tank_temp_init: float,
        config: MpcConfig,
        dhw_p1_blocked: list[bool],
        sh_locked_hours: set[int],
    ) -> list[bool]:
        """
        Phase 2 — DHW opportunistic pre-charging.

        Extends the Phase 1 schedule with cheap pre-charging hours, but only
        on hours that are **genuinely free** — not already committed to DHW
        (Phase 1) and not locked by SH scheduling (Phase 1 or Phase 2).

        Running after SH scheduling ensures that space-heating survival and
        opportunistic pre-charging always take priority over DHW opportunism.
        This prevents the long-horizon failure mode where DHW Phase 2 books
        cheap early hours before SH Phase 1 can use them to prevent tank
        depletion.

        An hour is added only when the DHW tank is materially below half its
        maximum capacity at that point in the simulation — the same guard used
        previously — to avoid booking hours that provide no meaningful benefit.

        Parameters
        ----------
        dhw_p1_blocked:
            Output of :py:meth:`_solve_dhw_phase1`; hours already committed
            to DHW survival scheduling.
        sh_locked_hours:
            Set of hour indices used by SH scheduling (``pump_on = True`` in
            the chosen SH schedule).

        Returns
        -------
        list[bool]
            Combined DHW schedule (Phase 1 + opportunistic Phase 2).
        """
        n, dhw_energy_init, dhw_max_energy, dhw_cap, dhw_cop = self._dhw_setup(
            horizon, dhw_tank_temp_init, config
        )

        def _dhw_simulate(dhw_on: list[bool]) -> tuple[list[dict], bool]:
            """Simulate DHW tank over the horizon.  Returns (states, feasible)."""
            e = dhw_energy_init
            states: list[dict] = []
            feasible = True
            for t in range(n):
                tank_start = e
                if dhw_on[t]:
                    headroom = max(0.0, dhw_max_energy - e)
                    heat_in = min(dhw_cap[t], headroom)
                else:
                    heat_in = 0.0
                e_end = e + heat_in - horizon[t].dhw_demand
                if e_end < 0.0:
                    feasible = False
                states.append({"tank_start": tank_start, "heat_in": heat_in, "tank_end": e_end})
                e = max(0.0, e_end)
            return states, feasible

        dhw_on = list(dhw_p1_blocked)  # Start from Phase 1 result; will be augmented.

        half_max_energy = dhw_max_energy * 0.5

        def _dhw_p2_cost(t: int) -> float:
            # Rank by price/COP, but penalise isolated starts: an hour that would
            # require a cold compressor start is ranked as if it costs an extra
            # start_penalty_kwh × price.  This naturally makes Phase 2 prefer
            # hours immediately after an existing SH or DHW P1 block (warm start),
            # causing DHW opportunistic hours to cluster at the end of SH cycles.
            prev_warm = t > 0 and (dhw_p1_blocked[t - 1] or (t - 1) in sh_locked_hours)
            start_cost = 0.0 if prev_warm else config.start_penalty_kwh * horizon[t].price
            return horizon[t].price / dhw_cop[t] + start_cost

        ranked = sorted(range(n), key=_dhw_p2_cost)
        for t in ranked:
            if dhw_on[t] or t in sh_locked_hours:
                continue
            dhw_on[t] = True
            states, _ = _dhw_simulate(dhw_on)
            if (states[t]["heat_in"] <= 0.0 or
                    states[t]["tank_start"] >= half_max_energy):
                dhw_on[t] = False

        return dhw_on

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def cost_per_kwh_heat(
        self,
        price: float,
        t_outdoor: float,
        rh: float,
        lwt: float,
    ) -> float:
        """
        Return the cost of delivering 1 kWh of heat at given conditions.

        This is the central decision metric used to rank hours.

        Parameters
        ----------
        price:
            Electricity price (currency / kWh).
        t_outdoor:
            Outdoor temperature (°C).
        rh:
            Relative humidity (%).
        lwt:
            Leaving water temperature setpoint (°C).

        Returns
        -------
        float
            Cost per kWh of delivered thermal energy (currency / kWh_th).
        """
        cop = max(self._model.get_effective_cop(t_outdoor, rh, lwt), 0.1)
        return price / cop

    # ------------------------------------------------------------------
    # Core optimisation entry point
    # ------------------------------------------------------------------

    def solve(
        self,
        horizon: list[HorizonPoint],
        tank_temp_init: float,
        config: MpcConfig,
        dhw_tank_temp_init: float | None = None,
    ) -> MpcResult:
        """
        Find the cost-optimal heat pump schedule over the given horizon.

        When ``config.dhw_enabled`` is True, the DHW pre-scheduler runs
        first to claim the cheapest hours for DHW reheating.  The remaining
        hours are then passed to the SH optimisation as usual.

        For each discrete SH LWT candidate the algorithm performs per-hour
        output selection (see module docstring).  The LWT with the lowest
        total electricity cost that satisfies all tank constraints is
        returned.  If no scenario is fully feasible the least-violated
        one is returned with ``feasible=False``.

        Parameters
        ----------
        horizon:
            Ordered list of :class:`HorizonPoint` objects, one per hour.
        tank_temp_init:
            SH buffer tank temperature at the start of the first hour (°C).
        config:
            Solver configuration.
        dhw_tank_temp_init:
            DHW tank temperature at the start of the horizon (°C).
            Required when ``config.dhw_enabled`` is True; ignored otherwise.
            Defaults to ``config.dhw_target_temp`` when omitted.

        Returns
        -------
        MpcResult
            The optimal (or best-effort) schedule and associated metadata.

        Raises
        ------
        ValueError
            If *horizon* is empty.
        """
        if not horizon:
            raise ValueError("horizon must contain at least one HorizonPoint.")

        kwh_per_k = _tank_kwh_per_k(config)
        tank_energy_init = max(0.0, (tank_temp_init - config.min_lwt) * kwh_per_k)

        # ------------------------------------------------------------------
        # DHW Phase 1: survival scheduling only.
        #
        # Books the minimum set of hours required to prevent the DHW tank
        # from running dry and to satisfy any ready-by constraints.  These
        # hours are blocked for the subsequent SH Phase 1 + Phase 2 pass.
        #
        # DHW Phase 2 (opportunistic pre-charging) is deferred until after
        # SH scheduling so it never steals hours that SH Phase 1 needs to
        # prevent the buffer tank from going empty.
        # ------------------------------------------------------------------
        if config.dhw_enabled:
            dhw_init = (
                dhw_tank_temp_init
                if dhw_tank_temp_init is not None
                else config.dhw_target_temp
            )
            dhw_p1_blocked = self._solve_dhw_phase1(horizon, dhw_init, config)
        else:
            dhw_init = config.dhw_target_temp  # never used; defined for later guards
            dhw_p1_blocked = [False] * len(horizon)

        lwt_list = _lwt_candidates(config)

        # Emission constraint: filter out LWT candidates that cannot transfer
        # the peak hourly thermal demand to the building.
        #
        # Physical model (pure thermal — no electrical energy involved):
        #   Q_thermal = k_emission × (LWT − t_room)   [kWh_th / h]
        # → LWT_min = t_room + peak_demand / k_emission
        #
        # k_emission is zero when no heating curve config is available;
        # in that case we skip the filter and fall back to tank-energy
        # feasibility only.
        if config.k_emission > 1e-6:
            peak_demand_kwh = max(h.house_demand for h in horizon)
            min_emission_lwt = config.t_room + peak_demand_kwh / config.k_emission
            filtered = [l for l in lwt_list if l >= min_emission_lwt - 1e-6]
            if filtered:
                lwt_list = filtered
            else:
                _LOGGER.warning(
                    "No LWT candidate satisfies emission constraint "
                    "(min required %.1f °C); evaluating all candidates.",
                    min_emission_lwt,
                )

        best_schedule: list[HourPlan] | None = None
        best_cost = float("inf")
        best_feasible = False
        best_lwt = lwt_list[0]
        best_dhw_final: list[bool] = list(dhw_p1_blocked)

        for lwt in lwt_list:
            schedule, cost, feasible = self._solve_scenario(
                horizon, config, tank_energy_init, lwt, dhw_p1_blocked
            )

            # ------------------------------------------------------------------
            # DHW Phase 2: run per-LWT so the true total cost (SH + DHW) drives
            # LWT selection.  Priority ordering: DHW P1 > SH P1 > SH P2 > DHW P2.
            # ------------------------------------------------------------------
            if config.dhw_enabled:
                sh_locked = {t for t, p in enumerate(schedule) if p.pump_on}
                dhw_candidate = self._apply_dhw_phase2(
                    horizon, dhw_init, config, dhw_p1_blocked, sh_locked
                )
                schedule = self._merge_dhw_into_schedule(
                    schedule, horizon, dhw_candidate, dhw_init, config
                )
                cost = sum(p.electricity_cost for p in schedule)
            else:
                dhw_candidate = list(dhw_p1_blocked)

            # Prefer feasible over infeasible; among equal feasibility pick cheaper.
            if best_schedule is None:
                accept = True
            elif feasible and not best_feasible:
                accept = True
            elif feasible == best_feasible and cost < best_cost:
                accept = True
            else:
                accept = False

            if accept:
                best_schedule = schedule
                best_cost = cost
                best_feasible = feasible
                best_lwt = lwt
                best_dhw_final = dhw_candidate

        dhw_final = best_dhw_final

        # optimal_output_kw: the action for the current hour (index 0).
        optimal_output_kw = (
            best_schedule[0].output_kw if best_schedule else config.heat_pump_output_kw
        )

        dhw_on_now = dhw_final[0] if dhw_final else False
        dhw_planned_hours = sum(dhw_final)

        if config.dhw_enabled:
            optimal_dhw_setpoint = (
                config.dhw_target_temp
                if dhw_on_now
                else config.dhw_min_temp - 1.0
            )
        else:
            optimal_dhw_setpoint = 0.0

        return MpcResult(
            feasible=best_feasible,
            optimal_lwt=best_lwt,
            optimal_output_kw=optimal_output_kw,
            total_cost=best_cost,
            schedule=best_schedule or [],
            dhw_on_now=dhw_on_now,
            optimal_dhw_setpoint=optimal_dhw_setpoint,
            dhw_planned_hours=dhw_planned_hours,
        )

    # ------------------------------------------------------------------
    # DHW → SH schedule merger
    # ------------------------------------------------------------------

    def _merge_dhw_into_schedule(
        self,
        schedule: list[HourPlan],
        horizon: list[HorizonPoint],
        dhw_blocked: list[bool],
        dhw_tank_temp_init: float,
        config: MpcConfig,
    ) -> list[HourPlan]:
        """
        Annotate each HourPlan with DHW state by re-simulating the DHW tank.

        The SH schedule already has ``pump_on=False`` for blocked hours;
        this pass adds ``dhw_on``, ``dhw_heat_delivered_kwh``,
        ``dhw_tank_energy_kwh``, and ``dhw_tank_temp_c``.
        """
        kwh_per_k_dhw = config.dhw_tank_volume_liters * _WATER_KWH_PER_LITRE_K
        dhw_max_energy = (config.dhw_target_temp - config.dhw_min_temp) * kwh_per_k_dhw
        e = max(0.0, (dhw_tank_temp_init - config.dhw_min_temp) * kwh_per_k_dhw)

        merged: list[HourPlan] = []
        for t, (plan, pt, is_dhw) in enumerate(zip(schedule, horizon, dhw_blocked)):
            if is_dhw:
                cap = self._model.get_max_output_at(
                    pt.t_outdoor, config.dhw_lwt, config.heat_pump_output_kw
                )
                headroom = max(0.0, dhw_max_energy - e)
                heat_in = min(cap, headroom)
            else:
                heat_in = 0.0

            e_end = e + heat_in - pt.dhw_demand
            dhw_tank_t = config.dhw_min_temp + max(0.0, e_end) / kwh_per_k_dhw

            # Actual combined compressor state from the previous hour, considering
            # both SH and the full DHW schedule (Phase 1 + Phase 2).
            prev_compressor_on = t > 0 and (schedule[t - 1].pump_on or dhw_blocked[t - 1])

            if is_dhw:
                # Compute DHW electricity cost — _solve_scenario recorded 0 for
                # these hours since it only tracks SH heat delivery.
                dhw_cop = max(
                    0.1,
                    self._model.get_effective_cop(pt.t_outdoor, pt.rh, config.dhw_lwt),
                )
                is_dhw_start = not prev_compressor_on
                elec_cost = (heat_in / dhw_cop) * pt.price
                if is_dhw_start and config.start_penalty_kwh > 0.0:
                    elec_cost += config.start_penalty_kwh * pt.price
                merged.append(
                    _dc_replace(
                        plan,
                        dhw_on=True,
                        dhw_heat_delivered_kwh=heat_in,
                        dhw_tank_energy_kwh=e_end,
                        dhw_tank_temp_c=dhw_tank_t,
                        electricity_cost=elec_cost,
                        start_event=is_dhw_start,
                    )
                )
            else:
                # SH or off hour.  Correct the start penalty charged by _solve_scenario
                # if a DHW Phase 2 hour was added immediately before this SH run —
                # the compressor was already warm, so no cold-start penalty applies.
                elec_cost = plan.electricity_cost
                is_start = plan.start_event
                if (
                    plan.pump_on
                    and plan.start_event
                    and prev_compressor_on
                    and config.start_penalty_kwh > 0.0
                ):
                    elec_cost -= config.start_penalty_kwh * pt.price
                    is_start = False
                merged.append(
                    _dc_replace(
                        plan,
                        dhw_on=False,
                        dhw_heat_delivered_kwh=0.0,
                        dhw_tank_energy_kwh=e_end,
                        dhw_tank_temp_c=dhw_tank_t,
                        electricity_cost=elec_cost,
                        start_event=is_start,
                    )
                )
            e = max(0.0, e_end)

        return merged

    # ------------------------------------------------------------------
    # Per-LWT scenario solver
    # ------------------------------------------------------------------

    def _solve_scenario(
        self,
        horizon: list[HorizonPoint],
        config: MpcConfig,
        tank_energy_init: float,
        lwt: float,
        dhw_blocked: list[bool] | None = None,
    ) -> tuple[list[HourPlan], float, bool]:
        """
        Evaluate a single LWT candidate with per-hour output selection.

        ``dhw_blocked[t]`` being True means the HP is committed to DHW mode
        that hour and cannot be used for SH — those hours are treated as
        forced-off for SH purposes.

        Returns (schedule, total_cost, feasible).
        """
        n = len(horizon)
        if dhw_blocked is None:
            dhw_blocked = [False] * n
        max_kw = config.heat_pump_output_kw
        min_kw = config.min_output_kw
        has_modulation = min_kw < max_kw - 0.1

        # ------------------------------------------------------------------
        # Build per-hour metrics for both output levels.
        # ------------------------------------------------------------------
        def _build_metrics(out_kw: float) -> list[dict]:
            result = []
            for pt in horizon:
                max_cap = self._model.get_max_output_at(pt.t_outdoor, lwt, max_kw)
                eff = min(out_kw, max_cap)
                cop = max(
                    self._model.get_cop_at_output(
                        pt.t_outdoor, pt.rh, lwt, eff, max_kw, min_kw
                    ),
                    0.1,
                )
                result.append(
                    {
                        "cop": cop,
                        "cost_per_kwh_heat": pt.price / cop,
                        "max_capacity_kw": max_cap,
                        "effective_output_kw": eff,
                    }
                )
            return result

        m_max = _build_metrics(max_kw)
        m_min = _build_metrics(min_kw) if has_modulation else m_max

        # Per-hour output choice: initialise to max (used in Phase 1).
        output_choice = [max_kw] * n  # max_kw or min_kw per hour

        def _active_metrics() -> list[dict]:
            return [m_min[t] if output_choice[t] == min_kw else m_max[t] for t in range(n)]

        # ------------------------------------------------------------------
        # Phase 1 — Constraint satisfaction using full output.
        #
        # Full output charges the tank fastest, minimising the number of
        # pump-on hours required to prevent the tank from going negative.
        # DHW-blocked hours are never eligible for SH scheduling.
        # ------------------------------------------------------------------
        pump_on = [False] * n
        for _ in range(n):
            states, feasible = self._simulate(horizon, m_max, pump_on, config, tank_energy_init, lwt)
            if feasible:
                break

            earliest_violation = next(
                (t for t, s in enumerate(states) if s["tank_end"] < 0.0), None
            )
            if earliest_violation is None:
                break

            candidates_before = [
                t for t in range(earliest_violation + 1)
                if not pump_on[t] and not dhw_blocked[t]
            ]
            if not candidates_before:
                break  # Cannot fix this violation — scenario stays infeasible.

            cheapest = min(candidates_before, key=lambda t: m_max[t]["cost_per_kwh_heat"])
            pump_on[cheapest] = True

        # ------------------------------------------------------------------
        # Post-Phase-1 downgrade
        #
        # Try to switch each Phase-1 pump-on hour from full output to
        # min output.  Min output has a better COP (modulation gain) so
        # the same heat costs less electricity.  Revert if the lower
        # output makes the schedule infeasible.
        # ------------------------------------------------------------------
        if has_modulation:
            for t in range(n):
                if not pump_on[t]:
                    continue
                output_choice[t] = min_kw
                _, still_ok = self._simulate(
                    horizon, _active_metrics(), pump_on, config, tank_energy_init, lwt
                )
                if not still_ok:
                    output_choice[t] = max_kw  # revert

        # ------------------------------------------------------------------
        # Phase 2 — Opportunistic pre-charging at min output.
        #
        # Consider off-hours for pre-charging, ranked cheapest COP first.
        # Because Phase 1 typically uses full output and fills the tank
        # quickly, most additional hours are rejected here (heat_in == 0
        # when the tank is full), naturally concentrating runtime in
        # high-COP windows.
        # ------------------------------------------------------------------
        ranked = sorted(range(n), key=lambda t: m_min[t]["cost_per_kwh_heat"])

        for t in ranked:
            if pump_on[t] or dhw_blocked[t]:
                continue
            pump_on[t] = True
            output_choice[t] = min_kw
            trial, _ = self._simulate(
                horizon, _active_metrics(), pump_on, config, tank_energy_init, lwt
            )
            if trial[t]["heat_in"] <= 0.0:
                # Tank was full at this hour — no heat delivered; revert.
                pump_on[t] = False
                output_choice[t] = max_kw

        # ------------------------------------------------------------------
        # Final authoritative simulation and schedule assembly.
        # ------------------------------------------------------------------
        final_metrics = _active_metrics()
        states, feasible = self._simulate(
            horizon, final_metrics, pump_on, config, tank_energy_init, lwt
        )

        kwh_per_k = _tank_kwh_per_k(config)
        total_cost = 0.0
        schedule: list[HourPlan] = []

        for t, (pt, s, m) in enumerate(zip(horizon, states, final_metrics)):
            heat_in = s["heat_in"]
            # Electrical cost for delivered heat (thermal kWh / COP × price).
            elec_cost = (heat_in / m["cop"]) * pt.price

            # Start penalty: charged once per off→on transition.
            # The penalty is in electrical kWh (compressor startup loss) × price.

            # The compressor gets a start penalty ONLY if it starts for SH,
            # and neither SH nor DHW was running the hour before.
            is_start = pump_on[t] and (t == 0 or not (pump_on[t - 1] or dhw_blocked[t - 1]))

            if is_start and config.start_penalty_kwh > 0.0:
                elec_cost += config.start_penalty_kwh * pt.price

            compressor_running_now = pump_on[t] or dhw_blocked[t]
            if t == 0:
                compressor_running_prev = False
            else:
                compressor_running_prev = pump_on[t - 1] or dhw_blocked[t - 1]

            start_event = compressor_running_now and not compressor_running_prev

            total_cost += elec_cost

            tank_end = s["tank_end"]
            tank_t = config.min_lwt + max(0.0, tank_end) / kwh_per_k

            schedule.append(
                HourPlan(
                    hour_index=t,
                    pump_on=pump_on[t],
                    lwt=lwt,
                    output_kw=m["effective_output_kw"],
                    max_capacity_kw=m["max_capacity_kw"],
                    cop_effective=m["cop"],
                    cost_per_kwh_heat=m["cost_per_kwh_heat"],
                    heat_delivered_kwh=heat_in,
                    tank_energy_kwh=tank_end,
                    tank_temp_c=tank_t,
                    electricity_cost=elec_cost,
                    start_event=start_event,
                )
            )

        return schedule, total_cost, feasible

    # ------------------------------------------------------------------
    # Tank physics simulation
    # ------------------------------------------------------------------

    def _simulate(
        self,
        horizon: list[HorizonPoint],
        metrics: list[dict],
        pump_on: list[bool],
        config: MpcConfig,
        tank_energy_init: float,
        lwt: float | None = None,
    ) -> tuple[list[dict], bool]:
        """
        Simulate tank energy over the horizon for a given pump schedule.

        Heat delivered per run hour is taken from ``metrics[t]["effective_output_kw"]``,
        which reflects the per-hour output choice and is already clamped to the
        pump's physical capacity.

        Charging is capped at the temperature the heat pump can actually deliver:
        a pump running at ``lwt`` cannot heat a tank that is already *above*
        that temperature (heat only flows from hot to cold).  The effective
        charging ceiling is therefore ``min(max_tank_temp, lwt)``.

        The tank may *start* above ``lwt`` (carried over from a previous run at
        a higher setpoint) — in that case heat_in is zero for the whole hour
        and the pump delivers nothing until demand drains the tank below lwt.

        When the tank is *at* (or below) the LWT ceiling the pump can deliver
        heat continuously even if the tank starts the hour right at the ceiling,
        because demand and standby losses drain the tank during the hour.  The
        effective headroom is therefore ``raw_headroom + house_demand + standby``.
        This is essential for low-mass / direct-coupled systems (e.g. fan coils
        with a ~50 L buffer) where the tiny tank would otherwise saturate within
        minutes at full output, leaving headroom = 0 and blocking all subsequent
        charging.

        ``tank_end`` may be negative — indicating a constraint violation —
        but the carry-forward value is clamped to zero so subsequent hours
        are not unfairly penalised.

        Parameters
        ----------
        lwt:
            Leaving water temperature for this scenario (°C).  Defaults to
            ``config.max_tank_temp`` when omitted (backward-compatible with tests
            that call this method directly without specifying an LWT).

        Returns
        -------
        (states, feasible)
            *states*: list of per-hour dicts with keys
            ``tank_start``, ``heat_in``, ``tank_end``.
            *feasible*: False if any ``tank_end`` is negative.
        """
        kwh_per_k = _tank_kwh_per_k(config)
        # Maximum energy the tank can hold at the given LWT.
        # If lwt < max_tank_temp the pump cannot charge the tank above lwt, so
        # the effective ceiling is min(max_tank_temp, lwt).
        effective_lwt = lwt if lwt is not None else config.max_tank_temp
        max_charge_energy = max(
            0.0,
            kwh_per_k * (min(config.max_tank_temp, effective_lwt) - config.min_lwt),
        )
        tank = tank_energy_init
        feasible = True
        states: list[dict] = []

        for t, pt in enumerate(horizon):
            tank_start = tank

            if pump_on[t]:
                # Per-hour deliverable output (already clamped to physical capacity).
                deliverable = metrics[t]["effective_output_kw"]
                # Raw headroom: negative when tank temperature exceeds the LWT
                # ceiling (pump water is cooler than tank; heat cannot flow uphill).
                raw_headroom = max_charge_energy - tank
                if raw_headroom >= 0.0:
                    # Tank is at or below the LWT ceiling.  During this 1-hour
                    # slot, demand and standby losses continuously drain the tank,
                    # creating room for the pump to deliver heat even when the tank
                    # starts the hour right at the ceiling.  This is especially
                    # critical for low-mass / direct-coupled systems (e.g. fan coils
                    # with a small buffer) where the tank saturates within minutes at
                    # full output: without accounting for demand drain, headroom stays
                    # zero for the rest of the horizon and the pump never runs again.
                    effective_headroom = raw_headroom + pt.house_demand + config.tank_standby_loss_kwh
                    heat_in = min(deliverable, effective_headroom)
                else:
                    # Tank is above the LWT ceiling — the pump delivers cooler water
                    # than the tank already holds; no heat transfer is possible.
                    heat_in = 0.0
            else:
                heat_in = 0.0

            tank_end = tank + heat_in - pt.house_demand - config.tank_standby_loss_kwh

            if tank_end < 0.0:
                feasible = False

            states.append(
                {
                    "tank_start": tank_start,
                    "heat_in": heat_in,
                    "tank_end": tank_end,
                }
            )

            # Clamp to zero so a single deficit does not cascade into all future hours.
            tank = max(0.0, tank_end)

        return states, feasible


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions, no state)
# ---------------------------------------------------------------------------


def _tank_kwh_per_k(config: MpcConfig) -> float:
    """Thermal capacity of the buffer tank (kWh per Kelvin)."""
    return config.tank_volume_liters * _WATER_KWH_PER_LITRE_K


def _max_tank_energy(config: MpcConfig) -> float:
    """Maximum storable energy in the tank above min_lwt (kWh)."""
    return _tank_kwh_per_k(config) * (config.max_tank_temp - config.min_lwt)


def _lwt_candidates(config: MpcConfig) -> list[float]:
    """Generate the discrete LWT candidates to evaluate."""
    candidates: list[float] = []
    lwt = config.min_lwt
    while lwt <= config.max_lwt + 1e-9:
        candidates.append(round(lwt, 6))
        lwt += config.lwt_step
    return candidates or [config.min_lwt]


def _output_candidates(config: MpcConfig) -> list[float]:
    """Return the two inverter output levels (min and max), deduplicated.

    Used by tests and diagnostics.  The solver itself performs per-hour
    output selection rather than evaluating whole-horizon scenarios at
    each output level.
    """
    max_kw = config.heat_pump_output_kw
    min_kw = config.min_output_kw
    if min_kw >= max_kw - 0.1:
        return [max_kw]
    return [min_kw, max_kw]
