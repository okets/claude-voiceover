"""The mod's bridge into the narrator: ops in, one JSON line out."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir, REPO_ROOT

data_dir = isolate()
from voiceover import lease, narrator, spool
from voiceover.settings import set_setting

set_setting("interaction_level", "narrator")
set_setting("tts_engine", "kokoro")
PROJECT = spool.queue_dir().parent


def bridge(*ops, session="S1", stdin=None, **env):
    payload = stdin if stdin is not None else json.dumps(
        {"session": session, "cwd": str(REPO_ROOT), "ops": list(ops)})
    return subprocess.run(["bash", str(REPO_ROOT / "hooks" / "run.sh"), "mod_bridge"],
                          input=payload, capture_output=True, text=True,
                          env=dict(os.environ, **env))


def reply(proc):
    return json.loads(proc.stdout.strip().splitlines()[-1])


def items():
    return [json.loads(p.read_text()) for p in sorted(spool.queue_dir().glob("*.json"))]


def leased(session="S1"):
    return (PROJECT / "leases" / (session + ".json")).exists()


# --- prompt: the gate, the mark, the pin, the cutover without a warm-up -------------
proc = bridge({"op": "prompt", "cutover": True})
check("the bridge exits 0", proc.returncode == 0, proc.stderr)
check("it prints exactly one line", len(proc.stdout.strip().splitlines()) == 1, proc.stdout)
check("prompt reports narration on", reply(proc) == {"narrate": True}, proc.stdout)
check("the batch marks the session", narrator.mod_active("S1"))
check("the prompt pins the session to its root", narrator.pinned_cwd("S1", None) == str(REPO_ROOT))
check("a cutover prompt files the session's stop request", (PROJECT / "stop" / "S1.json").exists())
check("but holds no engine: turn.start warms it", not leased())

spool.clear()
spool.enqueue("Mid-sentence.", "kokoro", "bf_emma", session="S1")
(PROJECT / "stop" / "S1.json").unlink()
bridge({"op": "prompt", "cutover": False})
check("a prompt that is not the user's own drops nothing", spool.pending_count() == 1)
check("and stops nothing", not (PROJECT / "stop" / "S1.json").exists())

# --- any op marks a session ---------------------------------------------------------------
bridge({"op": "status"}, session="AFTER-CLEAR")
check("a session id first seen in any op is marked", narrator.mod_active("AFTER-CLEAR"))

# --- hold, say: cleaned, one message, tagged --------------------------------------------
spool.clear()
bridge({"op": "hold"})
check("hold pre-warms: the session holds a lease", leased())
bridge({"op": "say", "utterance": "T:0", "text": "**Reading** the loader. ", "shown": 100.5},
       {"op": "say", "utterance": "T:0", "text": "---\n", "shown": 100.6},
       {"op": "say", "utterance": "T:0", "text": "**Then. ", "shown": 100.7})
queued = items()
check("each speakable segment is queued, markdown cleaned, stray markers stripped",
      [i["text"] for i in queued] == ["Reading the loader.", "Then."], queued)
check("segments of one response share its utterance", {i["utterance"] for i in queued} == {"T:0"})
check("the shown time and the source are kept for latency logging",
      queued and queued[0]["shown"] == 100.5 and queued[0]["source"] == "mod", queued)

# --- dialog: sticky, under the lead-in's utterance, lease released ------------------------
spool.clear()
bridge({"op": "dialog", "tool": "AskUserQuestion", "utterance": "T:0",
        "input": {"questions": [{"question": "Which approach?", "options": [{"label": "A"}]}]}})
alert = items()
check("the dialog alert is queued sticky under the lead-in's message",
      len(alert) == 1 and alert[0]["sticky"] is True and alert[0]["utterance"] == "T:0", alert)
check("it reads the actual question", alert and "Which approach?" in alert[0]["text"], alert)
check("the lease is released while the user decides", not leased())
check("this session's echo is suppressed", narrator.dialog_recently_announced("S1"))

# --- turn-end ---------------------------------------------------------------------------
bridge({"op": "hold"})
proc = bridge({"op": "turn-end", "spoken": False})
check("turn-end releases the lease", not leased())
check("a silent turn ends on the chime", "[voiceover] sound: decide" in proc.stderr, proc.stderr)

# --- narration turned off mid-turn still releases --------------------------------------------
bridge({"op": "hold"})
set_setting("interaction_level", "concise")
proc = bridge({"op": "turn-end", "spoken": True})
check("a turn end after narration was turned off still releases the engine", not leased())
lease.hold(None, "S1")
bridge({"op": "dialog", "tool": "ExitPlanMode", "input": {}, "utterance": None})
check("so does a dialog", not leased())

# --- below the narrator level, only the gate, the mark and the pin act --------------------
spool.clear()
proc = bridge({"op": "prompt", "cutover": True},
              {"op": "say", "utterance": "U", "text": "Not spoken."}, {"op": "hold"},
              session="S2")
check("concise: narration reported off", reply(proc) == {"narrate": False}, proc.stdout)
check("concise: nothing queued", items() == [], items())
check("concise: no lease taken", not leased("S2"))
check("concise: the session is still marked", narrator.mod_active("S2"))
set_setting("interaction_level", "narrator")

# --- headless runs never narrate ------------------------------------------------------------
spool.clear()
proc = bridge({"op": "hold"}, {"op": "say", "utterance": "H", "text": "Nobody listens."},
              session="H1", CLAUDE_CODE_ENTRYPOINT="sdk-cli")
check("a headless run reports narration off", reply(proc) == {"narrate": False}, proc.stdout)
check("and queues nothing, holds nothing", items() == [] and not leased("H1"), items())

# --- end-session ------------------------------------------------------------------------
bridge({"op": "hold"})
bridge({"op": "end-session"})
check("end-session unmarks the session", not narrator.mod_active("S1"))
check("end-session releases its lease", not leased())

# --- garbage in, one line out ------------------------------------------------------------
proc = bridge(stdin="not json")
check("a malformed batch still exits 0", proc.returncode == 0, proc.stderr)
check("and still prints one JSON line", "narrate" in reply(proc), proc.stdout)

# --- a prompt after a move to another project stops the old project's turn ------------------
other = temp_dir("vo-moved-")
(other / ".git").mkdir()


def bridge_from(cwd, session, *ops):
    return bridge(stdin=json.dumps({"session": session, "cwd": str(cwd), "ops": list(ops)}))


spool.clear()
bridge_from(REPO_ROOT, "S1", {"op": "start"})
spool.enqueue("Old turn, still talking.", "kokoro", "bf_emma", session="S1", cwd=str(REPO_ROOT))
old_stop = PROJECT / "stop" / "S1.json"
if old_stop.exists():
    old_stop.unlink()
bridge_from(other, "S1", {"op": "prompt", "cutover": True})
check("a prompt from another project drops the old project's pending items",
      spool.pending_count(cwd=str(REPO_ROOT)) == 0, spool.pending_count(cwd=str(REPO_ROOT)))
check("and files the stop request in the old project", old_stop.exists())

# --- below the narrator level the classic prompt hook owns the pin --------------------------
set_setting("interaction_level", "concise")
bridge_from(REPO_ROOT, "S2", {"op": "start"})
bridge_from(other, "S2", {"op": "prompt", "cutover": True})
check("a prompt op below the narrator level leaves the pin alone",
      narrator.pinned_cwd("S2", None) == str(REPO_ROOT), narrator.pinned_cwd("S2", None))
set_setting("interaction_level", "narrator")

# --- a moved root keeps the turn in the project its prompt pinned ----------------------------
spool.clear()
spool.clear(cwd=str(other))
bridge_from(REPO_ROOT, "S4", {"op": "prompt", "cutover": True}, {"op": "hold"})
bridge_from(other, "S4", {"op": "hold"}, {"op": "say", "utterance": "M:0", "text": "Still the repo's turn. ", "shown": 1.0})
check("a say after the root moved is queued in the pinned project",
      spool.pending_count(cwd=str(REPO_ROOT)) == 1, spool.pending_count(cwd=str(REPO_ROOT)))
check("and nothing is queued in the project the root moved to",
      spool.pending_count(cwd=str(other)) == 0, spool.pending_count(cwd=str(other)))
bridge_from(other, "S4", {"op": "turn-end", "spoken": True})
check("the turn end releases the lease in the pinned project", not leased("S4"))
check("and no lease was taken in the other project",
      not (spool.queue_dir(cwd=str(other)).parent / "leases" / "S4.json").exists())

# --- status keeps an existing pin -------------------------------------------------------------
bridge_from(REPO_ROOT, "S5", {"op": "start"})
bridge_from(other, "S5", {"op": "status"})
check("status leaves an existing pin alone",
      narrator.pinned_cwd("S5", None) == str(REPO_ROOT), narrator.pinned_cwd("S5", None))

# --- the user's prompt cuts over even with TTS just turned off ------------------------------
spool.clear()
set_setting("tts_enabled", False)
try:
    spool.enqueue("Old turn.", "kokoro", "bf_emma", session="S6", cwd=str(REPO_ROOT))
    bridge_from(REPO_ROOT, "S6", {"op": "prompt", "cutover": True})
    check("with TTS off the user's prompt still clears the old turn", spool.pending_count(cwd=str(REPO_ROOT)) == 0)
    check("and files the stop request", (PROJECT / "stop" / "S6.json").exists())
finally:
    set_setting("tts_enabled", True)

# --- a move out of a project below the narrator level: the gate is the root's -------------
from voiceover.settings import _project_settings_path
set_setting("interaction_level", "concise", scope="project", cwd=str(other))
try:
    bridge_from(other, "S9", {"op": "start"})
    proc = bridge(stdin=json.dumps({"session": "S9", "cwd": str(REPO_ROOT),
                                    "ops": [{"op": "prompt", "cutover": True}]}))
    check("a prompt that moves into a narrating project reports the root's gate",
          reply(proc) == {"narrate": True}, proc.stdout)
finally:
    _project_settings_path(str(other)).unlink(missing_ok=True)

# --- SessionEnd drops the mark even when the mod never says so -----------------------------
narrator.mark_mod_session("S3")
proc = subprocess.run(["bash", str(REPO_ROOT / "hooks" / "run.sh"), "session_end"],
                      input=json.dumps({"session_id": "S3", "cwd": str(REPO_ROOT)}),
                      capture_output=True, text=True, env=dict(os.environ))
check("the session_end hook exits 0", proc.returncode == 0, proc.stderr)
check("session end drops the mod mark", not narrator.mod_active("S3"))

report()
