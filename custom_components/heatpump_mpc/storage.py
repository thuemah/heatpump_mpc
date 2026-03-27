"""
Persistent storage for Heat Pump MPC learned state.

Uses ``homeassistant.helpers.storage.Store`` to write a JSON file under
``.storage/heatpump_mpc.<entry_id>`` that survives HA restarts.

Only the learned COP / capacity parameters need persistence — all other
coordinator state is rebuilt from live sensor readings on every update.

Schema
------
Version 1::

    {
        "learner": { ...CopLearnerState.to_dict()... }
    }

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

    async def async_save_learner_state(self, state: CopLearnerState) -> None:
        """
        Persist the current learner state to disk (debounced).

        Uses ``async_delay_save`` so rapid successive calls coalesce into a
        single write after ``_SAVE_DELAY_S`` seconds.
        """
        data = {
            "version": STORAGE_VERSION,
            "learner": state.to_dict(),
        }
        self._store.async_delay_save(lambda: data, _SAVE_DELAY_S)
