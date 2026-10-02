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
from voiceover import lease, narrator, spool
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
(spool.queue_dir().parent / "tts.lock").write_text(
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
(spool.queue_dir().parent / "tts.lock").unlink()

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

# --- the dialog branch announces and marks --------------------------------
dialog_marker = Path(os.environ["VOICEOVER_DATA_DIR"]) / "dialog_alert" / "S1"
dialog_marker.unlink(missing_ok=True)
spool.clear()
path = write_transcript("dialog.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("Let me check the options first.")) + "\n")
proc = run_hook("pre_tool_use", {
    "cwd": str(REPO_ROOT), "transcript_path": str(path),
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": [{"question": "Which approach?",
                                   "options": [{"label": "A"}, {"label": "B"}]}]},
    "session_id": "S1"})
check("pre_tool_use (dialog) exits 0", proc.returncode == 0, proc.stderr)
texts = queued_texts()
check("the dialog branch queues exactly one item", len(texts) == 1, texts)
check("the queued text carries both the prose and the alert",
      texts and "Let me check the options first." in texts[0]
      and "I need your input" in texts[0] and "Which approach?" in texts[0],
      texts)
check("the dialog marker was written", dialog_marker.exists(), dialog_marker)

# --- a failed enqueue must NOT write the marker (Finding A regression) ----
dialog_marker.unlink(missing_ok=True)
spool.clear()
path = write_transcript("dialog_fail.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("Never mind, checking again.")) + "\n")
set_setting("tts_engine", "none")
proc = run_hook("pre_tool_use", {
    "cwd": str(REPO_ROOT), "transcript_path": str(path),
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": [{"question": "Which one?", "options": []}]},
    "session_id": "S1"})
set_setting("tts_engine", "kokoro")
check("pre_tool_use (failed dialog enqueue) exits 0", proc.returncode == 0, proc.stderr)
check("nothing is queued when the enqueue fails", queued_texts() == [], queued_texts())
check("the marker is NOT written when the enqueue fails",
      not dialog_marker.exists(), dialog_marker.exists())

# --- notification.py's narrator path queues rather than speaks ------------
dialog_marker.unlink(missing_ok=True)
spool.clear()
path = write_transcript("notify.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
proc = run_hook("notification", {
    "cwd": str(REPO_ROOT), "transcript_path": str(path),
    "message": "Claude needs your permission to use Bash",
    "session_id": "S1"})
check("notification exits 0", proc.returncode == 0, proc.stderr)
check("notification queued its permission text", len(queued_texts()) == 1, queued_texts())

# ... and stays quiet when a fresh dialog marker says it was just announced
spool.clear()
narrator.write_dialog_marker("S1")
proc = run_hook("notification", {
    "cwd": str(REPO_ROOT), "transcript_path": str(path),
    "message": "Claude needs your permission to use Bash",
    "session_id": "S1"})
check("notification (echo-suppressed) exits 0", proc.returncode == 0, proc.stderr)
check("notification queues nothing when the dialog was just announced",
      queued_texts() == [], queued_texts())
dialog_marker.unlink(missing_ok=True)

def run_hook_env(name, payload, **env):
    """run_hook with extra environment variables for the hook process."""
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / (name + ".py"))],
        input=json.dumps(payload), capture_output=True, text=True,
        env=dict(os.environ, **env))


def lease_held(session="S1", cwd=None):
    return (spool.queue_dir(cwd).parent / "leases" / (session + ".json")).exists()


def pending_items():
    return [json.loads(p.read_text()) for p in sorted(spool.queue_dir().glob("*.json"))]


# --- the lease table, and an answered alert is retired (Review Focus 5) -----------------
spool.clear()
path = write_transcript("lease.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
base = {"cwd": str(REPO_ROOT), "transcript_path": str(path), "session_id": "S1"}
run_hook("pre_tool_use", dict(base, tool_name="Bash", tool_input={}))
check("a tool call holds the project's engine", lease_held())
run_hook("pre_tool_use", dict(base, tool_name="AskUserQuestion",
                              tool_input={"questions": [{"question": "Which?", "options": []}]}))
check("a question dialog releases it while the user decides", not lease_held())
items = pending_items()
check("the dialog alert is queued sticky", items and items[-1]["sticky"] is True, items)
check("and tagged as a dialog", items and items[-1]["source"] == "dialog", items)
run_hook("post_tool_use", dict(base, tool_name="AskUserQuestion", tool_input={}))
check("the answer takes it back", lease_held())
check("an alert answered before it played is retired",
      not any(i["sticky"] for i in pending_items()), pending_items())
run_hook("stop", base)
check("the turn's end lets it go", not lease_held())

# --- prose items carry their source and when they appeared ---------------------------
spool.clear()
with open(path, "a") as handle:
    entry = assistant("Stamped prose.")
    entry["timestamp"] = "2026-10-02T12:00:00.000Z"
    handle.write(json.dumps(entry) + "\n")
run_hook("pre_tool_use", dict(base, tool_name="Bash", tool_input={}))
items = pending_items()
check("a mid-turn item names the hook that queued it",
      items and items[0]["source"] == "pre_tool_use", items)
check("and when its prose appeared", items and isinstance(items[0]["shown"], float), items)
lease.release(None, "S1")

# --- the pin survives a cd (Review Focus 2) -------------------------------------------
from _harness import temp_dir
elsewhere = temp_dir("vo-elsewhere-")
(elsewhere / ".git").mkdir()
spool.clear()
run_hook("user_prompt_submit", {"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "go"})
run_hook("pre_tool_use", dict(base, cwd=str(elsewhere), tool_name="Bash", tool_input={}))
check("a tool call after a cd still holds the turn's own project", lease_held())
check("and holds nothing in the directory it moved to", not lease_held(cwd=str(elsewhere)))
run_hook("session_end", {"cwd": str(elsewhere), "session_id": "S1", "reason": "other"})
check("SessionEnd releases the lease where it was held", not lease_held())
check("and drops the pin", narrator.pinned_cwd("S1", None) is None)

# --- a background task's notification does not cut over (Review Focus 3) --------------
spool.clear()
spool.enqueue("Still talking.", "kokoro", "bf_emma", session="S1")
stop_dir = spool.queue_dir().parent / "stop"
for old in stop_dir.glob("*.json"):
    old.unlink()
run_hook("user_prompt_submit", {"cwd": str(REPO_ROOT), "session_id": "S1",
                                "prompt": "<task-notification>build finished</task-notification>"})
check("a task notification leaves the queue alone", spool.pending_count() == 1)
check("and requests no stop", not (stop_dir / "S1.json").exists())
check("but warms the engine for its turn", lease_held())
lease.release(None, "S1")
run_hook("user_prompt_submit", {"cwd": str(REPO_ROOT), "prompt": "no session id"})
check("a prompt without a session id stops no one", not (stop_dir / "_all.json").exists())

# --- permission requests are sticky; the dialog echo marker is per session --------------
spool.clear()
dialog_marker.unlink(missing_ok=True)   # the lease-table dialog above marked S1
narrator.write_dialog_marker("OTHER")
path = write_transcript("perm.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
run_hook("notification", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                          "message": "Claude needs your permission to use Bash",
                          "session_id": "S1"})
items = pending_items()
check("another session's dialog does not silence this session's permission request",
      len(items) == 1, items)
check("the permission request is sticky", items and items[0]["sticky"] is True, items)

# --- headless runs are not narrated -----------------------------------------------------
spool.clear()
path = write_transcript("headless.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("Nobody is listening.")) + "\n")
headless = {"cwd": str(REPO_ROOT), "transcript_path": str(path), "session_id": "H1"}
run_hook_env("user_prompt_submit", dict(headless, prompt="go"), CLAUDE_CODE_ENTRYPOINT="sdk-cli")
run_hook_env("pre_tool_use", dict(headless, tool_name="Bash", tool_input={}),
             CLAUDE_CODE_ENTRYPOINT="sdk-cli")
check("a headless run queues nothing", spool.pending_count() == 0, pending_items())
check("and holds no engine", not lease_held("H1"))

# --- a turn's end releases the engine at any level ----------------------------------------
lease.hold(None, "S1")
set_setting("interaction_level", "concise")
run_hook("stop", {"cwd": str(REPO_ROOT), "transcript_path": str(path), "session_id": "S1"})
set_setting("interaction_level", "narrator")
check("a stop after narration was turned off still releases the engine", not lease_held())

# --- every prompt re-pins its session, at any level ---------------------------------------
spool.clear()
set_setting("interaction_level", "silent", scope="project", cwd=str(elsewhere))
run_hook("user_prompt_submit", {"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "go"})
run_hook("user_prompt_submit", {"cwd": str(elsewhere), "session_id": "S1", "prompt": "go"})
check("a prompt from another project re-pins the session, at any level",
      narrator.pinned_cwd("S1", None) == str(elsewhere), narrator.pinned_cwd("S1", None))
path = write_transcript("repin.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("Nobody asked.")) + "\n")
run_hook("pre_tool_use", {"cwd": str(elsewhere), "transcript_path": str(path),
                          "session_id": "S1", "tool_name": "Bash", "tool_input": {}})
check("a silent project's session is not narrated by a stale pin",
      spool.pending_count() == 0 and spool.pending_count(cwd=str(elsewhere)) == 0)
(elsewhere / ".claude" / "voiceover.json").unlink()
narrator.unpin("S1")
lease.release(None, "S1")

# --- re-pinning releases the lease an interrupted turn left behind --------------------------
run_hook("user_prompt_submit", {"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "go"})
check("a prompt warms the engine in its project", lease_held())
run_hook("user_prompt_submit", {"cwd": str(elsewhere), "session_id": "S1", "prompt": "go"})
check("re-pinning releases the lease an interrupted turn left behind", not lease_held())
narrator.unpin("S1")
lease.release(None, "S1")
lease.release(str(elsewhere), "S1")

report()
