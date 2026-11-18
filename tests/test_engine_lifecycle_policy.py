"""LifecyclePolicy decisions with a fake clock."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
from engine_lifecycle import LifecyclePolicy, QuietInputs, quiet_exit_reason


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


clock = Clock()
p = LifecyclePolicy(idle_unload_s=10, quiet_check_s=5, clock=clock)
check("idle: not before idle_unload_s", not p.idle_due(busy=False))
clock.now += 10
check("idle: after idle_unload_s", p.idle_due(busy=False))
check("idle: never while busy", not p.idle_due(busy=True))
p.note_activity()
check("idle: activity resets the clock", not p.idle_due(busy=False) and p.idle_s(busy=False) == 0)
p.hold(30)
clock.now += 20
check("warm: hold outlasts idle", not p.idle_due(busy=False) and p.leases() == 1)
check("warm: warm_remaining", p.warm_remaining() == 10)
clock.now += 11
check("warm: after the hold the idle clock decides", p.idle_due(busy=False) and p.leases() == 0
      and p.warm_remaining() is None)
check("hold: capped at 600", (p.hold(9999), p.warm_remaining())[1] == 600)
check("hold: negative treated as 0", p.hold(-5) is None)

q = LifecyclePolicy(idle_unload_s=10, quiet_check_s=5, clock=clock)
check("quiet: not due immediately", not q.quiet_due())
clock.now += 5
check("quiet: due after the interval, then resets", q.quiet_due() and not q.quiet_due())

base = dict(plugin_root="/r", start_device=None, pointer=None, settings={}, engine_status={}, keep_off=None)
check("reason: nothing", quiet_exit_reason(QuietInputs(**base)) is None)
check("reason: pointer elsewhere", quiet_exit_reason(QuietInputs(**dict(base, pointer={"plugin_root": "/o"}))) == "update")
check("reason: pointer same", quiet_exit_reason(QuietInputs(**dict(base, pointer={"plugin_root": "/r"}))) is None)
check("reason: setting elsewhere", quiet_exit_reason(QuietInputs(**dict(base, settings={"engine_plugin_root": "/o"}))) == "update")
check("reason: device changed", quiet_exit_reason(QuietInputs(**dict(base, start_device="cpu", settings={"tts_device": "cuda"})))
      == "update")
fb = {"fallback": {"reason": "keep_off", "at": 1}}
check("reason: card_return when keep-off expired", quiet_exit_reason(QuietInputs(**dict(base, engine_status=fb))) == "card_return")
check("reason: keep-off still in force", quiet_exit_reason(QuietInputs(**dict(base, engine_status=fb, keep_off={"until": 1}))) is None)
check("reason: other fallback stays", quiet_exit_reason(QuietInputs(
    **dict(base, engine_status={"fallback": {"reason": "load_failed"}}))) is None)
report()
