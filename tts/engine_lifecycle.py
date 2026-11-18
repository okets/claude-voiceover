# tts/engine_lifecycle.py
"""When the shared Kokoro engine may leave: pure decision logic. Stdlib only.

LifecyclePolicy tracks activity and warm holds against an injectable clock.
quiet_exit_reason decides the quiet-moment exits from facts the server gathers.
The server owns the timer and the wiring; nothing here touches sockets or files.
"""

import time
from dataclasses import dataclass, field

MAX_HOLD_S = 600.0
IDLE_TICK_S = 0.25


@dataclass
class QuietInputs:
    """The outside facts a quiet-moment check looks at."""
    plugin_root: str
    start_device: object
    pointer: dict = None
    settings: dict = field(default_factory=dict)
    engine_status: dict = field(default_factory=dict)
    keep_off: dict = None


def _plugin_moved(inputs):
    mine = str(inputs.plugin_root)
    pointed = (inputs.pointer or {}).get("plugin_root")
    configured = inputs.settings.get("engine_plugin_root")
    return (pointed is not None and str(pointed) != mine) or (configured is not None and str(configured) != mine)


def _device_changed(inputs):
    return inputs.settings.get("tts_device") != inputs.start_device


def _keep_off_over(inputs):
    fallback = inputs.engine_status.get("fallback") or {}
    return fallback.get("reason") == "keep_off" and inputs.keep_off is None


def quiet_exit_reason(inputs):
    """"update", "card_return" or None."""
    if _plugin_moved(inputs) or _device_changed(inputs):
        return "update"
    return "card_return" if _keep_off_over(inputs) else None


class LifecyclePolicy:
    def __init__(self, idle_unload_s, quiet_check_s, clock=time.monotonic):
        self.idle_unload_s = idle_unload_s
        self.quiet_check_s = quiet_check_s
        self.tick_s = min(quiet_check_s, IDLE_TICK_S)
        self._clock = clock
        self._started = clock()
        self._last_activity = self._started
        self._last_quiet_check = self._started
        self._holds = []  # expiry times

    def note_activity(self):
        self._last_activity = self._clock()

    def hold(self, hold_s):
        """Keep the engine loaded for hold_s (capped); counts as activity."""
        self.note_activity()
        hold_s = min(max(hold_s, 0.0), MAX_HOLD_S)
        if hold_s > 0:
            self._holds.append(self._clock() + hold_s)

    def _live_holds(self):
        now = self._clock()
        self._holds = [until for until in self._holds if until > now]
        return self._holds

    def leases(self):
        return len(self._live_holds())

    def warm_remaining(self):
        """Seconds until the last hold ends, or None when none is active."""
        holds = self._live_holds()
        return max(holds) - self._clock() if holds else None

    def idle_s(self, busy):
        return 0.0 if busy else self._clock() - self._last_activity

    def uptime_s(self):
        return self._clock() - self._started

    def idle_due(self, busy):
        return not busy and not self._live_holds() and self.idle_s(busy) >= self.idle_unload_s

    def quiet_due(self):
        """True once per quiet_check_s."""
        now = self._clock()
        if now - self._last_quiet_check < self.quiet_check_s:
            return False
        self._last_quiet_check = now
        return True
