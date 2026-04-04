"""
Persistent storage for Heat Pump MPC learned state.

Uses ``homeassistant.helpers.storage.Store`` to write a JSON file under
``.storage/heatpump_mpc.<entry_id>`` that survives HA restarts.

Schema
------
Version 1::

    {
        "learner": { ...CopLearnerState.to_dict()... },
        "sh_total_kwh_th": <float>,
        "sh_hourly_buffer": [{"datetime": "<ISO>", "kwh_th_sh": <float>}, ...]
    }

The ``sh_*`` keys are optional for backward compatibility — missing keys are
treated as zeros / empty lists so existing stores migrate silently.

Upgrading
---------
If a schema change is needed in a future version, increment ``STORAGE_VERSION``
and add a migration branch in :py:meth:`HeatpumpMpcStorage.async_load`.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .core.cop_learner import CopLearnerState

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "heatpump_mpc"
STORAGE_VERSION = 1

# Delay (seconds) between the last write request and the actual disk write.
# Prevents hammering the filesystem when observations arrive in quick succession.
_SAVE_DELAY_S = 30


class HeatpumpMpcStorage:
    """
    Thin, async-safe wrapper around ``homeassistant.helpers.storage.Store``.

    One instance per config entry; keyed by ``entry_id`` so multiple MPC
    instances coexist without collisions.

    Parameters
    ----------
    hass:
        Home Assistant instance.
    entry_id:
        The config entry's ``entry_id``.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY}.{entry_id}",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_load_learner_state(self) -> CopLearnerState:
        """
        Load persisted learner state from disk.

        Returns a cold-start :class:`CopLearnerState` (datasheet defaults) if
        no data has been saved yet or if the stored data is unreadable.
        """
        try:
            raw: dict[str, Any] | None = await self._store.async_load()
        except Exception as err:
            _LOGGER.warning(
                "Failed to load persisted learner state: %s — using cold-start defaults.",
                err,
            )
            return CopLearnerState()

        if raw is None:
            _LOGGER.debug("No persisted learner state found — starting from datasheet defaults.")
            return CopLearnerState()

        version = raw.get("version", 1)
        if version != STORAGE_VERSION:
            _LOGGER.warning(
                "Learner storage version mismatch (stored=%s expected=%s) — "
                "resetting to defaults.",
                version,
                STORAGE_VERSION,
            )
            return CopLearnerState()

        learner_dict = raw.get("learner")
        if not isinstance(learner_dict, dict):
            _LOGGER.warning("Stored learner data is malformed — resetting to defaults.")
            return CopLearnerState()

        try:
            state = CopLearnerState.from_dict(learner_dict)
            _LOGGER.debug(
                "Loaded learner state: η=%.4f f_defrost=%.4f "
                "cap@-15°C=%.3f (n=%d) cap@-7°C=%.3f (n=%d) "
                "clean_samples=%d",
                state.eta_carnot,
                state.f_defrost,
                state.capacity_frac_minus15,
                state.capacity_minus15_samples,
                state.capacity_frac_minus7,
                state.capacity_minus7_samples,
                state.eta_carnot_samples,
            )
            return state
        except Exception as err:
            _LOGGER.warning(
                "Cannot deserialise learner state (%s) — resetting to defaults.", err
            )
            return CopLearnerState()

    async def async_load_sh_state(self) -> tuple[float, list[dict]]:
        """
        Load persisted SH thermal energy state from disk.

        Returns ``(0.0, [])`` when no data has been saved yet or when the
        stored data is unreadable.  Missing ``sh_*`` keys in older stores are
        treated as a cold start (zero total, empty buffer).
        """
        try:
            raw: dict | None = await self._store.async_load()
        except Exception as err:
            _LOGGER.warning("Failed to load SH state: %s — cold-starting.", err)
            return 0.0, []

        if raw is None:
            return 0.0, []

        version = raw.get("version", 1)
        if version != STORAGE_VERSION:
            return 0.0, []

        total = raw.get("sh_total_kwh_th", 0.0)
        buffer = raw.get("sh_hourly_buffer", [])

        try:
            total = float(total)
        except (TypeError, ValueError):
            total = 0.0

        if not isinstance(buffer, list):
            buffer = []

        _LOGGER.debug(
            "Loaded SH state: total=%.3f kWh_th, buffer=%d entries.",
            total,
            len(buffer),
        )
        return total, buffer

    async def async_save_learner_state(self, state: CopLearnerState) -> None:
        """
        Persist the current learner state to disk (debounced).

        Uses ``async_delay_save`` so rapid successive calls coalesce into a
        single write after ``_SAVE_DELAY_S`` seconds.

        .. deprecated::
            Prefer :py:meth:`schedule_full_save` which also persists SH state.
        """
        data = {
            "version": STORAGE_VERSION,
            "learner": state.to_dict(),
        }
        self._store.async_delay_save(lambda: data, _SAVE_DELAY_S)

    def schedule_full_save(
        self,
        learner_state: CopLearnerState,
        sh_total_kwh_th: float,
        sh_hourly_buffer: list[dict],
        sh_pending_hour: dict | None = None,
    ) -> None:
        """
        Schedule a debounced write of all persistent state (sync, safe to call
        from synchronous code inside the event loop).

        Combines learner parameters and SH hourly buffer into one write.
        Rapid successive calls coalesce into a single disk write after
        ``_SAVE_DELAY_S`` seconds.

        Parameters
        ----------
        sh_pending_hour:
            Snapshot of the in-progress hour accumulator so that it survives
            HA restarts.  Keys: ``hour_start`` (ISO string), ``kwh_th``,
            ``kwh_el``, ``sh_windows``, ``dhw_windows``.
            Pass ``None`` to omit (backward-compatible).
        """
        # Capture values in a local dict so the lambda does not close over
        # mutable references that could change before the write fires.
        snapshot: dict = {
            "version": STORAGE_VERSION,
            "learner": learner_state.to_dict(),
            "sh_total_kwh_th": sh_total_kwh_th,
            "sh_hourly_buffer": list(sh_hourly_buffer),
        }
        if sh_pending_hour is not None:
            snapshot["sh_pending_hour"] = sh_pending_hour
        self._store.async_delay_save(lambda: snapshot, _SAVE_DELAY_S)

    async def async_load_sh_pending_hour(self) -> dict | None:
        """Load the in-progress hour accumulator snapshot from disk.

        Returns ``None`` when no pending hour has been saved (first run or
        clean hour boundary).
        """
        try:
            raw: dict | None = await self._store.async_load()
        except Exception:
            return None

        if raw is None:
            return None

        return raw.get("sh_pending_hour")
