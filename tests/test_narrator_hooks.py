"""The narrator hook paths queue their text and never lose it to the lock."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

data_dir = isolate()
from voiceover import spool
from voiceover.settings import set_setting

set_setting("interaction_level", "narrator")
set_setting("tts_engine", "kokoro")
set_setting("voice", "bf_emma")

transcripts = Path(os.environ["VOICEOVER_DATA_DIR"]) / "tx"
transcripts.mkdir(parents=True, exist_ok=True)


def write_transcript(name, entries):
    path = transcripts / name
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return path


def assistant(text):
    return {"type": "assistant", "isSidechain": False,
            "message": {"role": "assistant", "type": "message",
                        "model": "claude-fable-5-1",
                        "content": [{"type": "text", "text": text}]}}


USER = {"type": "user", "message": {"role": "user", "content": "go"}}


def run_hook(name, payload):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / (name + ".py"))],
        input=json.dumps(payload), capture_output=True, text=True,
        env=os.environ.copy())
    return proc


def queued_texts():
    out = []
    for path in sorted(spool.queue_dir().glob("*.json")):
        out.append(json.loads(path.read_text())["text"])
    return out


# --- post_tool_use queues Claude's prose -----------------------------------
spool.clear()
path = write_transcript("post.jsonl", [USER])
from voiceover.prose import commit_offset
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("Reading the settings loader now.")) + "\n")
proc = run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                                  "tool_name": "Bash", "tool_input": {},
                                  "session_id": "S1"})
check("post_tool_use exits 0", proc.returncode == 0, proc.stderr)
check("post_tool_use queued the prose",
      queued_texts() == ["Reading the settings loader now."], queued_texts())

# --- THE POINT: a held lock no longer costs the text ----------------------
spool.clear()
path = write_transcript("locked.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("This must not be lost.")) + "\n")
(Path(os.environ["VOICEOVER_DATA_DIR"]) / "tts.lock").write_text(
    json.dumps({"expiry": time.time() + 60, "pid": 1}))
proc = run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                                  "tool_name": "Bash", "tool_input": {},
                                  "session_id": "S1"})
check("the hook still exits 0 while audio plays", proc.returncode == 0, proc.stderr)
check("the prose was queued, not skipped",
      queued_texts() == ["This must not be lost."], queued_texts())

# --- and the cursor advanced, so it is not read twice --------------------
proc = run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                                  "tool_name": "Bash", "tool_input": {},
                                  "session_id": "S1"})
check("the same prose is not queued a second time",
      queued_texts() == ["This must not be lost."], queued_texts())
(Path(os.environ["VOICEOVER_DATA_DIR"]) / "tts.lock").unlink()

# --- pre_tool_use queues too ----------------------------------------------
spool.clear()
path = write_transcript("pre.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("About to run the tests.")) + "\n")
proc = run_hook("pre_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                                 "tool_name": "Bash", "tool_input": {},
                                 "session_id": "S1"})
check("pre_tool_use exits 0", proc.returncode == 0, proc.stderr)
check("pre_tool_use queued the prose",
      queued_texts() == ["About to run the tests."], queued_texts())

# --- stop queues the finale, and does not interrupt ----------------------
spool.clear()
path = write_transcript("stop.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("All done, tests are green.")) + "\n")
    handle.write(json.dumps(assistant("Here is the summary.")) + "\n")
proc = run_hook("stop", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                         "session_id": "S1"})
check("stop exits 0", proc.returncode == 0, proc.stderr)
check("stop queued the closing prose", len(queued_texts()) >= 1, queued_texts())

# --- status notices still never get queued (regression) ------------------
spool.clear()
path = write_transcript("synthetic.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps({
        "type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
        "apiErrorStatus": 429,
        "message": {"role": "assistant", "type": "message", "model": "<synthetic>",
                    "content": [{"type": "text",
                                 "text": "You've hit your session limit"}]}}) + "\n")
run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                           "tool_name": "Bash", "tool_input": {}, "session_id": "S1"})
check("a session-limit notice is never queued", spool.pending_count() == 0,
      queued_texts())

# --- items are tagged with the session -----------------------------------
spool.clear()
path = write_transcript("session.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("Tagged with a session.")) + "\n")
run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                           "tool_name": "Bash", "tool_input": {},
                           "session_id": "SESSION-XYZ"})
item = json.loads(sorted(spool.queue_dir().glob("*.json"))[0].read_text())
check("the hook records its session id", item["session"] == "SESSION-XYZ", item)

report()
