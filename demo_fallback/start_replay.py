r"""Start the labelled fallback: the web page in REPLAY mode, serving a recording of a real earlier run.

    .venv\Scripts\python.exe demo_fallback\start_replay.py [recording.jsonl] [demo-app-url]

Opens nothing by itself: browse to http://127.0.0.1:8001/ (the page shows a full-width REPLAY banner).
Defaults: rehearsal 5's recording and the seeded demo app on http://127.0.0.1:8765/ (start that first).
Costs nothing: replay mode has no API client. See docs/DEMO_RUNBOOK.md and D17.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8001
recording = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "runs" / "web_c5333263" / "llm_record.jsonl"
demo_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765/"

if not recording.is_file():
    sys.exit(f"recording not found: {recording}")

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
