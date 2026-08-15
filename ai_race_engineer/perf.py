"""Process priority, so the engineer never competes with the sim.

AIRE is idle almost all the time, but answering a question briefly costs real
CPU — speech decoding, the radio DSP, and audio playback all land in the same
second. Average load is under half a percent of a modern CPU, which sounds
harmless, but sim racing cares about the worst frame, not the average one: a
burst that lands on the core running iRacing's render thread is a stutter the
driver feels.

Dropping to below-normal priority makes Windows hand the CPU to the sim
whenever the two want it at once. It costs nothing when the machine is idle —
priority only decides who wins under contention, not how fast a thread runs —
so the engineer answers just as quickly with a core to spare.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("race-engineer")

# From Windows' SetPriorityClass. Below normal, but above idle, so the app
# still gets scheduled promptly when the sim is not asking for the core.
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_NORMAL_PRIORITY_CLASS = 0x00000020


def lower_priority(enabled: bool = True) -> bool:
    """Drop this process below normal priority. Returns whether it applied.

    Never raises: failing to set priority is a missed optimisation, not a
    reason to stop the app from starting.
    """
    if not enabled:
        return False
    if sys.platform != "win32":
        # Linux/macOS would need nice(), which needs no privileges to lower
        # but is not useful without a sim to yield to.
        logger.debug("Priority tuning is Windows-only; leaving it alone")
        return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # The signatures matter. GetCurrentProcess returns the pseudo-handle
        # (HANDLE)-1, and without an explicit restype ctypes truncates it to a
        # 32-bit int, so the call silently fails on 64-bit Windows.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.GetPriorityClass.restype = wintypes.DWORD
        kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]

        handle = kernel32.GetCurrentProcess()
        if not kernel32.SetPriorityClass(handle, _BELOW_NORMAL_PRIORITY_CLASS):
            raise OSError(ctypes.get_last_error(), "SetPriorityClass failed")
        applied = kernel32.GetPriorityClass(handle)
    except Exception as exc:                       # noqa: BLE001 — see docstring
        # Warn, not debug: this failed silently once already, and a missing
        # optimisation you cannot see is one nobody fixes.
        logger.warning("Could not lower process priority: %s", exc)
        return False

    if applied != _BELOW_NORMAL_PRIORITY_CLASS:
        logger.warning("Priority did not stick (class is 0x%X)", applied)
        return False

    logger.info("Running at below-normal priority so the sim always wins the CPU")
    return True
