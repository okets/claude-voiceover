#!/usr/bin/env python3
"""Claude Voiceover first-run setup.

Usage:
    setup_tts.py                Print a status report and next steps
    setup_tts.py --download     Download the Kokoro models (~300MB, one time)
    setup_tts.py --test         Speak a sample line through the resolved engine

Checks tooling (uv, audio playback), reuses an existing ~/.kokoro-tts cache,
and never re-downloads models that are already present. Stdlib only.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from voiceover import settings  # noqa: E402

KOKORO_DIR = Path.home() / ".kokoro-tts" / "models"
MODEL_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}

LINUX_PLAYERS = ["ffplay", "aplay", "paplay", "mpv"]


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
                  shutil.which("uv") or "", "needed for Kokoro voices - install: https://docs.astral.sh/uv/")
    player = audio_player_found()
    check("audio playback", player is not None, str(player), "install ffmpeg (ffplay) or another audio player")
    if system == "Darwin":
        check("macOS 'say' voices", shutil.which("say") is not None, "instant, zero-setup narration")
    print()
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
    return models_ok and uv_ok


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
    if engine == "kokoro":
        command = ["uv", "run", "--project", str(PLUGIN_ROOT / "tts"),
                   str(PLUGIN_ROOT / "tts" / "kokoro_voice.py"), settings.get_voice(), text]
    elif engine in ("macos-female", "macos-male"):
        voice = "Samantha" if engine == "macos-female" else "Daniel"
        command = [sys.executable, str(PLUGIN_ROOT / "tts" / "macos_say.py"), "--voice", voice, text]
    else:
        print("No usable engine yet - download Kokoro models or switch to a macOS voice.")
        return False
    print(f"Speaking a test line via {engine}...")
    try:
        return subprocess.run(command, timeout=120).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        print(f"Audio test failed: {error}")
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setup_tts.py", description="Claude Voiceover first-run setup")
    parser.add_argument("--download", action="store_true", help="Download Kokoro models (~300MB, skips files already cached)")
    parser.add_argument("--test", action="store_true", help="Speak a sample line to verify audio")
    args = parser.parse_args(argv)

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
    if not args.download and not ready:
        print()
        print("Next step: setup_tts.py --download, or pick a macOS voice with /voiceover:voice default-female")
    return 0


if __name__ == "__main__":
    sys.exit(main())
