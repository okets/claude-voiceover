"""The one-shot interrupt never reaches a narrator engine or its playback.

Checked in-process against the real command lines - pkill -f matches each
pattern as an extended regex anywhere in the command line, Windows matches a
lowercase substring - so the test never kills anything on this machine."""

import re
import sys
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
}
ONE_SHOTS = {
    "the one-shot kokoro engine": "uv run --project /p/tts /p/tts/kokoro_voice.py --voice bf_emma --stream hello",
    "a voice preview": "uv run --project /p/tts /p/tts/kokoro_voice.py --voice af_nicole Hello there",
    "the one-shot say engine": "python3 /p/tts/macos_say.py --voice Samantha hello",
    "the one-shot say child": "say -v Samantha",
    "the one-shot wav playback": "afplay /var/folders/xy/T/voiceover-oneshot-abc.wav",
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
          re.search(r'kokoro_voice\.py"\),\s*"--voice"', source) is not None)

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

report()
