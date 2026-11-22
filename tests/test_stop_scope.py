"""The one-shot interrupt never reaches a narrator engine or its playback.

Checked in-process against the real command lines - pkill -f matches each
pattern as an extended regex anywhere in the command line, Windows matches a
lowercase substring - so the test never kills anything on this machine."""

import json
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

data_dir = isolate()
from voiceover import process_utils

PATTERNS = process_utils.TTS_PROCESS_PATTERNS


def unix_kills(cmdline):
    return any(re.search(pattern, cmdline) for pattern in PATTERNS)


def windows_kills(cmdline):
    return any(pattern.lower() in cmdline.lower() for pattern in PATTERNS)


DRAINERS = {
    "a kokoro drainer": "uv run --project /p/tts /p/tts/kokoro_voice.py --drain /d/projects/abc",
    "a macOS drainer": "python3 /p/tts/macos_say.py --drain /d/projects/abc",
    "a drainer's say": "say --voice=Samantha",
    "a drainer's afplay": "afplay /d/projects/abc/play/12-3.wav",
    "a drainer's aplay": "aplay -q /d/projects/abc/play/12-3.wav",
    "a Phonon drainer": "uv run --project /p/tts-phonon /p/tts/phonon_voice.py --drain /d/projects/abc",
    "a Phonon drainer's piped player": "aplay -q -t raw -f FLOAT_LE -r 24000 -c 1 -",
}
ONE_SHOTS = {
    "the one-shot kokoro engine": "uv run --project /p/tts /p/tts/kokoro_voice.py --voice bf_emma --stream hello",
    "a voice preview": "uv run --project /p/tts /p/tts/kokoro_voice.py --voice af_nicole Hello there",
    "the one-shot say engine": "python3 /p/tts/macos_say.py --voice Samantha hello",
    "the one-shot say child": "say -v Samantha",
    "the one-shot wav playback": "afplay /var/folders/xy/T/voiceover-oneshot-abc.wav",
    "the one-shot Phonon engine": "uv run --project /p/tts-phonon /p/tts/phonon_voice.py --voice Bryony --stream Hi.",
    "a Phonon one-shot in a multi-word voice":
        "uv run --project /p/tts-phonon /p/tts/phonon_voice.py --voice Captain Silas Blackwater --stream Hi.",
}
for name, cmdline in DRAINERS.items():
    check(name + " survives the one-shot interrupt",
          not unix_kills(cmdline) and not windows_kills(cmdline), cmdline)
for name, cmdline in ONE_SHOTS.items():
    check(name + " is stopped by it", unix_kills(cmdline), cmdline)

# --- every one-shot caller uses the --voice form the patterns match ---------------
for script in ("scripts/manage_voices.py", "scripts/setup_tts.py"):
    source = (REPO_ROOT / script).read_text()
    check(script + " calls kokoro_voice.py with --voice",
          re.search(r'kokoro_voice\.py"\),\s*"--voice"|kokoro_command\(\s*"--voice"', source) is not None)

# --- the kill switch reaches every project and clears its state --------------------
sys.path.insert(0, str(REPO_ROOT / "tts"))
import tts_controller

project = data_dir / "projects" / "abc123def456"
for relative in ("tts.lock", "stop/S1.json", "queue/1.json", "leases/S1.json", "play/1.wav"):
    (project / relative).parent.mkdir(parents=True, exist_ok=True)
    (project / relative).write_text("x")
check("the kill switch looks at every project's lock",
      project / "tts.lock" in tts_controller.lock_paths(), tts_controller.lock_paths())
tts_controller.clear_projects()
check("and leaves no lock, queue, lease, stop file or wav behind",
      [p for p in project.rglob("*") if p.is_file()] == [],
      [str(p) for p in project.rglob("*") if p.is_file()])
check("the kill switch also stops a drainer's say and aplay",
      any(re.search(p, "say --voice=Samantha") for p in tts_controller.UNIX_PATTERNS)
      and any(re.search(p, "aplay -q /d/projects/abc/play/1.wav") for p in tts_controller.UNIX_PATTERNS))


def controller_kills(cmdline):
    """Unix pkill -f and Windows -match both treat the patterns as regexes."""
    return (any(re.search(p, cmdline) for p in tts_controller.UNIX_PATTERNS)
            and any(re.search(p, cmdline) for p in tts_controller.WINDOWS_PATTERNS))


check("the kill switch stops a Phonon drainer, a waiting newcomer included",
      controller_kills("uv run --project /p/tts-phonon /p/tts/phonon_voice.py --drain /d/projects/abc"))
check("the kill switch stops a Phonon one-shot",
      controller_kills("uv run --project /p/tts-phonon /p/tts/phonon_voice.py --voice Old Mortlake --stream Hi."))


def group_killed(module, command):
    """Whether module's lock-owner kill signals the group of a lock owner running command."""
    lock = data_dir / "owner.lock"
    lock.write_text(json.dumps({"expiry": 9e12, "pid": 424242}))
    killed = []
    real_run, real_killpg, real_getpgid = module.subprocess.run, module.os.killpg, module.os.getpgid
    module.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=command + "\n")
    module.os.killpg = lambda pgid, sig: killed.append(pgid)
    module.os.getpgid = lambda pid: pid
    try:
        if module is tts_controller:
            real_paths = module.lock_paths
            module.lock_paths = lambda: [lock]
            try:
                module._kill_lock_owner_groups()
            finally:
                module.lock_paths = real_paths
        else:
            module._kill_lock_owner_group(lock)
    finally:
        module.subprocess.run, module.os.killpg, module.os.getpgid = real_run, real_killpg, real_getpgid
    return killed == [424242]


if sys.platform != "win32":
    owner = "/u/.venv/bin/python3 /p/tts/phonon_voice.py --drain /d/projects/abc"
    check("the kill switch recognises a Phonon lock owner", group_killed(tts_controller, owner))
    check("the one-shot interrupt recognises a Phonon lock owner", group_killed(process_utils, owner))
    check("and neither signals an unrelated process that reused the pid",
          not group_killed(tts_controller, "/usr/bin/vim notes.txt")
          and not group_killed(process_utils, "/usr/bin/vim notes.txt"))

report()
