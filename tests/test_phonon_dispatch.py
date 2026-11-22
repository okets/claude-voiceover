"""Phonon dispatch: the command lines, the queued item, and which engine a hook starts.

Isolated from the checkout it runs in: every project is a temp directory of
its own (a .git marker) whose own settings name its engine and voice, passed
as cwd. Spawns are captured, never run.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir, REPO_ROOT

isolate()
from voiceover import projects, settings, speech, spool


def new_project(engine, voice=None):
    root = temp_dir("vo-phonon-dispatch-")
    (root / ".git").mkdir()
    settings.set_setting("tts_engine", engine, scope="project", cwd=str(root))
    settings.set_setting("voice", voice, scope="project", cwd=str(root))
    return str(root)


def queued_items(cwd):
    return [json.loads(path.read_text()) for path in sorted(spool.queue_dir(cwd=cwd).glob("*.json"))]


spawned = []
real_spawn = speech._spawn_detached
speech._spawn_detached = lambda command: spawned.append(command) or True
try:
    # --- the command lines --------------------------------------------------------
    cmd = speech.phonon_command("--install")
    check("the Phonon command runs from its own uv project",
          cmd[:4] == ["uv", "run", "--project", str(REPO_ROOT / "tts-phonon")]
          and cmd[4] == str(REPO_ROOT / "tts" / "phonon_voice.py") and cmd[5:] == ["--install"], cmd)
    phonon_cwd = new_project("phonon", "Bryony")
    root = projects.project_dir(cwd=phonon_cwd)
    cmd = speech._drain_command("phonon", root)
    check("the drain command names the project directory",
          cmd[:5] == speech.phonon_command()[:5] and cmd[-2:] == ["--drain", str(root)], cmd)

    # --- what a Phonon project queues ----------------------------------------------
    check("enqueue_speech queues for a Phonon project", speech.enqueue_speech("Hello there.", cwd=phonon_cwd))
    item = queued_items(phonon_cwd)[-1]
    check("a queued Phonon item carries its engine and voice",
          (item["engine"], item["voice"]) == ("phonon", "Bryony"), item)
    auto_cwd = new_project("phonon")
    speech.enqueue_speech("Hello there.", cwd=auto_cwd)
    item = queued_items(auto_cwd)[-1]
    check("with no explicit voice it queues one of the 25 standard voices",
          item["engine"] == "phonon" and item["voice"] in settings.PHONON_VOICES, item)

    # --- which engine a hook starts --------------------------------------------------
    spawned.clear()
    check("ensure_drainer starts an engine for a Phonon project", speech.ensure_drainer(phonon_cwd) is True)
    check("ensure_drainer starts the Phonon engine for a Phonon project",
          spawned and spawned[0][4] == str(REPO_ROOT / "tts" / "phonon_voice.py")
          and spawned[0][5:] == ["--drain", str(root)], spawned)
    kokoro_cwd = new_project("kokoro", "bf_emma")
    kokoro_cmd = speech._drain_command("kokoro", projects.project_dir(cwd=kokoro_cwd))
    check("a Kokoro project still gets the Kokoro command", "kokoro_voice.py" in kokoro_cmd[4], kokoro_cmd)
    macos_cmd = speech._drain_command("macos-female", projects.project_dir(cwd=kokoro_cwd))
    check("a macOS project still gets the macOS command",
          macos_cmd[:2] == ["python3", str(REPO_ROOT / "tts" / "macos_say.py")] and macos_cmd[-2] == "--drain",
          macos_cmd)
    check("no engine, no command", speech._drain_command("none", root) is None)

    # --- the one-shot speaker ----------------------------------------------------------
    spawned.clear()
    speech._dispatch("Hi.", "phonon", phonon_cwd)
    check("the one-shot runs the Phonon engine with the project's voice",
          spawned == [speech.phonon_command("--voice", "Bryony", "--stream", "Hi.")], spawned)
    character_cwd = new_project("phonon", "Captain Silas Blackwater")
    spawned.clear()
    speech._dispatch("Ahoy.", "phonon", character_cwd)
    check("a multi-word voice is passed as one argument",
          spawned and spawned[0][-4:] == ["--voice", "Captain Silas Blackwater", "--stream", "Ahoy."], spawned)
    spawned.clear()
    speech._dispatch("Hi.", "kokoro", kokoro_cwd)
    check("the Kokoro one-shot is unchanged",
          spawned == [speech.kokoro_command("--voice", "bf_emma", "--stream", "Hi.")], spawned)
    spawned.clear()
    speech._dispatch("Hi.", "macos-male", kokoro_cwd)
    check("the macOS one-shot is unchanged",
          spawned == [["python3", str(REPO_ROOT / "tts" / "macos_say.py"), "--voice", "Daniel", "Hi."]], spawned)
finally:
    speech._spawn_detached = real_spawn

report()
