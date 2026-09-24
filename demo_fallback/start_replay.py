r"""Start the labelled fallback: the web page in REPLAY mode, serving a recording of a real earlier run.

    .venv\Scripts\python.exe demo_fallback\start_replay.py [recording.jsonl] [demo-app-url]

Opens nothing by itself: browse to http://127.0.0.1:8001/ (the page shows a full-width REPLAY banner).
Default: the committed fixture (rehearsal 5, pinned by hash, D18) and the seeded demo app on
http://127.0.0.1:8765/ (start that first). A different recording may be given for practice; it is
announced as NOT the pinned fallback. Costs nothing: replay mode has no API client. See
docs/DEMO_RUNBOOK.md, D17 and D18.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8001
FIXTURE = ROOT / "demo_fallback" / "fixture" / "rehearsal5_llm_record.jsonl"
PINNED_SHA256 = "bda600a35bdf1c475eebf2ae41a92db9e20e187dc037b7f0e047695bfbcd3067"
recording = Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURE
demo_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765/"

if not recording.is_file():
    sys.exit(f"recording not found: {recording}")
digest = hashlib.sha256(recording.read_bytes()).hexdigest()
if recording == FIXTURE and digest != PINNED_SHA256:
    sys.exit("fallback fixture hash mismatch - do not use it (D18)")
print("pinned fallback OK sha256=" + digest[:8] if recording == FIXTURE else f"NOT the pinned fallback: {recording} sha256={digest[:8]}")

env = dict(os.environ)
env.update({
    "PROBE_LLM_MODE": "replay",
    "PROBE_LLM_SOURCE": str(recording),
    "PROBE_REPLAY_URL": demo_url,
    # The server default is ONE mission, and one mission of this recording finds nothing (D17).
    "PROBE_REPLAY_MAX_MISSIONS": "3",
})
child = subprocess.Popen([sys.executable, "-m", "uvicorn", "web.server:app", "--port", str(PORT)], cwd=ROOT, env=env)
print(f"replay server pid={child.pid}  ->  http://127.0.0.1:{PORT}/   (Ctrl-C stops it)", flush=True)
try:
    child.wait()
except KeyboardInterrupt:
    child.terminate()
