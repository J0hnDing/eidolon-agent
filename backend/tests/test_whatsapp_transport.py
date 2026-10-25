import subprocess
import sys
import threading
from base64 import b64encode
from pathlib import Path
from shutil import rmtree, which
from uuid import uuid4

import pytest

from app.services.whatsapp_bridge import MAX_FRAME, WhatsAppBridge, WhatsAppError


def child_bridge(tmp_path, source, timeout=2):
    script = tmp_path / "fake_child.py"
    script.write_text(source, encoding="utf-8")
    bridge = WhatsAppBridge(tmp_path, timeout=timeout)
    process = subprocess.Popen([sys.executable, "-u", str(script)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bridge._process = process
    threading.Thread(target=bridge._read, args=(process,), daemon=True).start()
    return bridge


def test_ndjson_correlates_responses_and_stops_on_parent_close(tmp_path):
    bridge = child_bridge(tmp_path, '''import json,sys
for line in sys.stdin:
    request=json.loads(line)
    print(json.dumps({"event":"status","data":{"status":"pairing"}}),flush=True)
    print(json.dumps({"id":request["id"],"result":{"method":request["method"]}}),flush=True)
''')
    process = bridge._process
    try:
        assert bridge.request("status") == {"method": "status"}
        assert bridge.snapshot == {"status": "pairing"}
        with pytest.raises(WhatsAppError) as exc:
            bridge.request("execute", {"value": "x" * MAX_FRAME})
        assert exc.value.error_type == "invalid_input"
    finally:
        bridge.stop()
    assert process.poll() is not None


@pytest.mark.parametrize("source", [
    "import sys;sys.stdin.readline();sys.exit(0)",
    "import sys,time;sys.stdin.readline();time.sleep(3)",
    "import sys;sys.stdin.readline();sys.stdout.write('x'*4194305);sys.stdout.flush()",
])
def test_crash_timeout_or_oversized_response_never_claims_send_failure(tmp_path, source):
    bridge = child_bridge(tmp_path, source, timeout=0.1)
    try:
        with pytest.raises(WhatsAppError) as exc:
            bridge.request("execute", {"operation": "whatsapp.message.send"})
        assert exc.value.error_type == "outcome_unknown"
    finally:
        bridge.stop()


def test_missing_dependencies_has_actionable_sanitized_error(tmp_path):
    bridge = WhatsAppBridge(tmp_path)
    with pytest.raises(WhatsAppError) as exc:
        bridge.start(key="PRIVATE_KEY_MUST_NOT_APPEAR", generation="test")
    assert exc.value.error_type == "dependencies_missing"
    assert "PRIVATE" not in str(exc.value)


def test_real_bridge_initializes_and_stops_without_pairing():
    root = Path(__file__).resolve().parents[2]
    if not which("node") or not (root / "bridges/whatsapp/node_modules/@whiskeysockets/baileys").exists():
        pytest.skip("Optional Node/Baileys dependencies are not installed")
    bridge = WhatsAppBridge(root)
    generation = uuid4().hex
    directory = root / "runtime" / "whatsapp" / generation
    try:
        bridge.start(key=b64encode(b"k" * 32).decode(), generation=generation)
        status = bridge.request("status")
        assert status["connected"] is False
        assert status["status"] == "disconnected"
        assert status["qr_data_url"] is None
        assert status["account_id"] is None
    finally:
        bridge.stop()
        if directory.exists():
            assert directory.resolve().parent == (root / "runtime" / "whatsapp").resolve()
            rmtree(directory)
