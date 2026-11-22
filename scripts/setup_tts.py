#!/usr/bin/env python3
"""Claude Voiceover first-run setup.

Usage:
    setup_tts.py                    Print a status report and next steps
    setup_tts.py --download-phonon  Install the Phonon voices (~65MB model, one time)
    setup_tts.py --download         Download the Kokoro models (~300MB, one time)
    setup_tts.py --test             Speak a sample line through the resolved engine
    setup_tts.py --share-engine [PORT]   Linux: serve the engine to other programs (systemd)
    setup_tts.py --unshare-engine        Undo --share-engine

Checks tooling (uv, audio playback), reuses an existing ~/.kokoro-tts cache,
and never re-downloads models that are already present. Installing an engine
never selects it: the engine changes only when the user picks a voice.
Stdlib only (sounddevice lives only in the Phonon environment).
"""

import argparse
import ctypes.util
import os
import platform
import shutil
import getpass
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.append(str(PLUGIN_ROOT / "tts"))

import engine_paths  # noqa: E402
from voiceover import engine_pointer, settings  # noqa: E402
from voiceover.speech import kokoro_command, one_shot_command, phonon_command  # noqa: E402
from pcm_sink import PLAYER_NAMES  # noqa: E402  (stdlib only; never imports sounddevice)

KOKORO_DIR = Path.home() / ".kokoro-tts" / "models"
MODEL_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}

FIRST_SHARE_PORT = 47320
LAST_PORT = 65535
AUTO_PORT = "auto"
TOKEN_NOTE = ("TCP callers need the engine's token. To keep it out of this transcript, run this in your own "
              "terminal and put the token in your assistant's secrets:\n  %s token")

LINUX_PLAYERS = ["ffplay", "aplay", "paplay", "mpv"]
PHONON_INSTALL_TIMEOUT = 600  # the first run also builds the tts-phonon environment
RECOMMENDED_PHONON = "zoey"   # placeholder until the user picks one after hearing them
NOT_SELECTED = "Phonon is installed but not selected. Pick a voice with /voiceover:voice."


def check(label, ok, detail_ok="", detail_missing=""):
    """Print one status line and return ok unchanged."""
    mark = "[ok]" if ok else "[--]"
    detail = detail_ok if ok else detail_missing
    print(f"  {mark} {label}" + (f" - {detail}" if detail else ""))
    return ok


def audio_player_found():
    """Best available audio playback tool for this platform."""
    system = platform.system()
    if system == "Darwin":
        return shutil.which("afplay")
    if system == "Windows":
        return "winsound"  # stdlib, always present
    for player in LINUX_PLAYERS:
        found = shutil.which(player)
        if found:
            return found
    return None


def portaudio_found():
    """Whether the PortAudio library sounddevice needs is installed (Linux)."""
    return ctypes.util.find_library("portaudio") is not None


def phonon_audio_status():
    """(ok, detail) for Phonon's streaming output on this platform. macOS and
    Windows get PortAudio from the sounddevice wheel; Linux needs the system
    library or one of the raw-audio players the engine can pipe to."""
    if platform.system() in ("Darwin", "Windows"):
        return True, "PortAudio (bundled with the Phonon environment)"
    if portaudio_found():
        return True, "PortAudio"
    players = [name for name in PLAYER_NAMES if shutil.which(name)]
    if players:
        return True, "piped to " + players[0]
    return False, ("no PortAudio and no player - install one of " + ", ".join(PLAYER_NAMES)
                   + " (e.g. alsa-utils for aplay, or ffmpeg for ffplay), or libportaudio2")


def phonon_selected():
    return settings.resolve_engine() == "phonon"


def print_phonon_status():
    """Phonon's block of the report. Returns True when its model is installed."""
    print("Phonon neural voices (37 voices, local and free, about 65MB):")
    installed = check("Phonon model", settings.phonon_model_present(), "installed", "not downloaded")
    audio_ok, detail = phonon_audio_status()
    check("Phonon audio output", audio_ok, detail, detail)
    print()
    if not installed:
        print("Phonon is not installed. Run: setup_tts.py --download-phonon  (~65MB, one time)")
    elif not phonon_selected():
        print(NOT_SELECTED)
    else:
        print("Phonon is installed and selected.")
    print()
    return installed


def model_status():
    """Return (all_present, list of (name, path, present, size_mb))."""
    rows = []
    all_present = True
    for name in MODEL_FILES:
        path = KOKORO_DIR / name
        present = path.exists() and path.stat().st_size > 0
        size_mb = path.stat().st_size / 1_000_000 if present else 0
        rows.append((name, path, present, size_mb))
        all_present = all_present and present
    return all_present, rows


def print_status():
    """Full environment report. Returns True when Kokoro is fully ready."""
    system = platform.system()
    print("Claude Voiceover setup status")
    print()
    print(f"  Platform: {system} ({platform.machine()}), Python {platform.python_version()}")
    print()
    print("Tooling:")
    uv_ok = check("uv package manager", shutil.which("uv") is not None,
                  shutil.which("uv") or "", "needed for Phonon and Kokoro voices - install: https://docs.astral.sh/uv/")
    player = audio_player_found()
    check("audio playback", player is not None, str(player), "install ffmpeg (ffplay) or another audio player")
    if system == "Darwin":
        check("macOS 'say' voices", shutil.which("say") is not None, "instant, zero-setup narration")
    print()
    phonon_ok = print_phonon_status()
    print("Kokoro neural voices (13 voices, local and free):")
    models_ok, rows = model_status()
    for name, path, present, size_mb in rows:
        check(name, present, f"{size_mb:.0f}MB at {path}", "not downloaded")
    print()
    if models_ok:
        print("Kokoro models are installed (existing cache reused - nothing to download).")
    else:
        print("Kokoro models are not installed. Run: setup_tts.py --download  (~300MB, one time)")
    print()
    print(f"Resolved engine: {settings.resolve_engine()}")
    print(f"Resolved voice:  {settings.get_voice()}")
    print(f"TTS device:      {settings.get_machine_setting('tts_device')}"
          " (gpu = NVIDIA CUDA via the tts-gpu environment; manage_settings.py set tts_device gpu)")
    return (models_ok or phonon_ok) and uv_ok


def install_phonon():
    """Run the Phonon engine's --install through uv (the first run also builds
    its environment), its output on the terminal. Selects nothing."""
    print("Installing Phonon (builds its environment and downloads the ~65MB model)...")
    try:
        code = subprocess.run(phonon_command("--install"), timeout=PHONON_INSTALL_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print(f"Phonon install failed: timed out after {PHONON_INSTALL_TIMEOUT}s.")
        return False
    except OSError as error:
        print(f"Phonon install failed: could not run uv ({error}). Install uv: https://docs.astral.sh/uv/")
        return False
    if code != 0:
        print(f"Phonon install failed (exit {code}); the engine's error is above.")
        return False
    print("Phonon installed. Your current voice is unchanged.")
    print(f"Next step: pick a voice with /voiceover:voice (for example {RECOMMENDED_PHONON}).")
    return True


def download_progress(name):
    """Progress-printing hook for urlretrieve."""
    def report(blocks, block_size, total_size):
        done = blocks * block_size
        if total_size > 0:
            percent = min(100, done * 100 // total_size)
            sys.stdout.write(f"\r  {name}: {percent}% ({done / 1_000_000:.0f}MB)")
        else:
            sys.stdout.write(f"\r  {name}: {done / 1_000_000:.0f}MB")
        sys.stdout.flush()
    return report


def download_models():
    """Fetch any missing model files. Existing files are never re-downloaded."""
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    _, rows = model_status()
    for (name, path, present, _size) in rows:
        if present:
            print(f"  {name}: already present, skipping download")
            continue
        url = MODEL_FILES[name]
        print(f"  Downloading {name}...")
        partial = path.with_suffix(path.suffix + ".part")
        try:
            urllib.request.urlretrieve(url, partial, reporthook=download_progress(name))
            print()
            partial.replace(path)
            print(f"  Saved {path}")
        except Exception as error:
            print(f"\n  Download failed for {name}: {error}")
            if partial.exists():
                partial.unlink()
            return False
    print("Kokoro models installed.")
    return True


def speak_test():
    """Verify audio end-to-end with a sample line through the resolved engine."""
    text = "Claude Voiceover is ready."
    if os.environ.get("VOICEOVER_DRY_RUN"):
        print(f"[voiceover] {text}", file=sys.stderr)
        return True
    engine = settings.resolve_engine()
    command = one_shot_command(engine, settings.get_voice(), text, stream=False)
    if command is None:
        if settings.phonon_model_present():
            print(NOT_SELECTED)
        else:
            print("No usable engine yet - install Phonon or Kokoro, or switch to a macOS voice.")
        return False
    print(f"Speaking a test line via {engine}...")
    try:
        return subprocess.run(command, timeout=120).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        print(f"Audio test failed: {error}")
        return False


def port_is_free(port):
    """True when 127.0.0.1:port can be bound right now (the probe socket is closed at once)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def first_free_port(start=FIRST_SHARE_PORT):
    return next((port for port in range(start, LAST_PORT + 1) if port_is_free(port)), None)


def run_loginctl(*args):
    """loginctl's stdout, or None when it cannot run. Tests replace this."""
    try:
        done = subprocess.run(["loginctl", *args], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def linger_enabled(user):
    out = run_loginctl("show-user", user, "-p", "Linger")
    return out is not None and out.strip() == "Linger=yes"


def warn_if_no_linger():
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()
    if linger_enabled(user):
        return
    print("Warning: lingering is off for %s, so the engine stops when you log out." % user)
    print("  To keep it available after logout, run:  loginctl enable-linger %s" % user)


def _port_arg(text):
    return text if text == AUTO_PORT else int(text)


def _engine_launcher():
    """The launcher module, imported only when sharing: it needs fcntl, which Windows lacks."""
    import engine_launcher
    return engine_launcher


def _launcher(*argv):
    return _engine_launcher().main(list(argv))


def _choose_port(requested):
    """(port, error): the requested port if usable, else our configured one, else the first free.
    The configured port counts as available: our own socket unit holds it."""
    configured = engine_paths.engine_port()
    if requested == AUTO_PORT:
        return (configured if configured is not None else first_free_port()), None
    if not 1 <= requested <= LAST_PORT:
        return None, "Port %d is outside 1-%d." % (requested, LAST_PORT)
    if requested != configured and not port_is_free(requested):
        return None, "Port %d is already in use on 127.0.0.1; pick another with --share-engine PORT." % requested
    return requested, None


def _install_everything(port):
    """prepare, shim, units; an error message, or None."""
    settings.set_setting("engine_port", port)
    project = _engine_launcher().default_project(PLUGIN_ROOT, engine_paths.read_global_settings())
    if _launcher("prepare", str(PLUGIN_ROOT), "--project", str(project)) != 0:
        return "Could not prepare the engine environment (is uv installed?)."
    engine_pointer.install_shim()
    if _launcher("install-service") != 0:
        return "Could not install the systemd units."
    return None


def share_engine(requested_port=AUTO_PORT):
    """Opt in to sharing the engine: port, prepared environment, systemd units, token (created, never
    printed). Returns an exit code."""
    if not sys.platform.startswith("linux"):
        print("--share-engine is Linux only (it uses systemd user units).")
        return 1
    port, error = _choose_port(requested_port)
    if error is None and port is None:
        error = "No free TCP port found for the engine."
    previous = settings.get_machine_setting("engine_port")
    error = error or _install_everything(port)
    if error:
        settings.set_setting("engine_port", previous)
        print(error)
        return 1
    print("Engine shared on 127.0.0.1:%d and %s" % (port, engine_paths.socket_path()))
    warn_if_no_linger()
    engine_paths.read_or_create_token()   # created now, shown only by `voiceover-engine token`
    print(TOKEN_NOTE % engine_paths.shim_path())
    return 0


def unshare_engine():
    code = _launcher("uninstall-service")
    settings.set_setting("engine_port", None)
    _launcher("stop")
    print("Engine no longer shared." if code == 0 else "Could not remove the systemd units.")
    return 0 if code == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setup_tts.py", description="Claude Voiceover first-run setup")
    parser.add_argument("--download-phonon", action="store_true",
                        help="Install the Phonon voices (~65MB model; builds its environment)")
    parser.add_argument("--download", action="store_true", help="Download Kokoro models (~300MB, skips files already cached)")
    parser.add_argument("--test", action="store_true", help="Speak a sample line to verify audio")
    parser.add_argument("--share-engine", nargs="?", const=AUTO_PORT, type=_port_arg, metavar="PORT",
                        help="Linux: share the engine over a systemd socket (and 127.0.0.1:PORT; default first free from 47320)")
    parser.add_argument("--unshare-engine", action="store_true", help="Remove the systemd units and the engine port")
    args = parser.parse_args(argv)

    if args.share_engine is not None:
        return share_engine(args.share_engine)
    if args.unshare_engine:
        return unshare_engine()
    if args.download_phonon:
        if not install_phonon():
            return 1
        print()
    if args.download:
        ok = download_models()
        if not ok:
            return 1
        print()
    ready = print_status()
    if args.test:
        print()
        if not speak_test():
            return 1
    if not (args.download or args.download_phonon) and not ready:
        print()
        print("Next step: setup_tts.py --download-phonon (Phonon, ~65MB) or --download (Kokoro, ~300MB),"
              " or pick a macOS voice with /voiceover:voice default-female")
    return 0


if __name__ == "__main__":
    sys.exit(main())
