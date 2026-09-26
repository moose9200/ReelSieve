"""Container supervisor: one child exiting stops its peer and fails the container."""
import signal
import sys
import time

from app import start


def test_crashed_child_stops_peer_and_fails(tmp_path):
    marker = tmp_path / 'peer-stopped'
    peer = [sys.executable, '-c', f'import signal,sys,time,pathlib\n'
            f'signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path({str(marker)!r}).write_text("1"), sys.exit(0)))\n'
            f'time.sleep(60)']
    crash = [sys.executable, '-c', 'import time; time.sleep(0.5); raise SystemExit(3)']
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        began = time.time()
        assert start.supervise([peer, crash], grace=10) == 1
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    assert marker.read_text() == '1' and time.time() - began < 15


def test_worker_only_health_and_role_flags(db, monkeypatch):
    import json
    import urllib.request
    monkeypatch.setenv('WEB_ENABLED', '0')
    monkeypatch.setenv('WORKER_ENABLED', '1')
    assert [c[2] for c in start.commands()] == ['app.worker']
    monkeypatch.setenv('WORKER_ENABLED', '0')
    import pytest
    with pytest.raises(SystemExit):
        start.commands()
    server = start.health_server(0, 'test-build')
    try:
        body = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/healthz', timeout=5).read())
        assert body == {'ok': True, 'role': 'worker', 'build': 'test-build', 'db': True}
    finally:
        server.shutdown()
