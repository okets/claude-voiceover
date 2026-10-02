#!/usr/bin/env python3
"""SessionEnd hook: let go of the session's engine lease and its pin.

The fallback's only reliable end for a session: Stop does not fire when the
user interrupts, and an exit would otherwise hold the lease until its TTL.
Never blocks, never prints to stdout, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    from voiceover import lease, narrator

    session = payload.get("session_id")
    lease.release(narrator.pinned_cwd(session, payload.get("cwd")), session)
    narrator.unpin(session)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
