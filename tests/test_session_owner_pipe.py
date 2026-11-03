import json
import subprocess
import sys
from pathlib import Path


def test_session_owner_accepts_full_campaign_sized_requests():
    # a full-campaign prepare request with an expanded enemy plan is about 30 MiB
    request = json.dumps({"action": "observe", "room": {"padding": "x" * (32 * 1024 * 1024)}}) + "\n"
    completed = subprocess.run(
        [sys.executable, "-m", "doom_eap.launcher.session_owner_app"],
        input=request.encode(), capture_output=True, timeout=120,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    response = json.loads(completed.stdout.decode().splitlines()[0])
    assert response["error"] == "Prepare a session before observing it"
