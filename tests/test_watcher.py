"""Windows recording watcher must distinguish success, failure, and unknown."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")
@pytest.mark.parametrize("exit_code, expected", [(0, "complete"), (3, "failed")])
def test_watcher_exit_codes(tmp_path, exit_code, expected):
    children = []
    try:
        for duration, code in [(30, 0), (4, 0), (4, exit_code)]:
            children.append(subprocess.Popen([
                sys.executable, "-c", f"import time; time.sleep({duration}); raise SystemExit({code})"],
                creationflags=subprocess.CREATE_NO_WINDOW))
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"stage": "recording"}), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "finish-recording.ps1"
        subprocess.run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-BackendPid", str(children[0].pid), "-TrafficPid", str(children[1].pid),
            "-CollectorPid", str(children[2].pid), "-StatusPath", str(status)],
            check=True, timeout=20, capture_output=True)
        result = json.loads(status.read_text(encoding="utf-8-sig"))
        assert result["stage"] == expected
        assert result["collector_exit_code"] == exit_code
        assert result["traffic_exit_code"] == 0
        assert children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
