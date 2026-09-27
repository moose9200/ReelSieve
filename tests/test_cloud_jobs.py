"""Durable jobs and the render worker against real isolated PostgreSQL.

The paid/expensive render is replaced by small child processes that speak the same JSON-line
protocol; Google is the synthetic HTTP fake. Nothing here calls a paid provider or sends messages.
"""
import os
import sys
import threading
import time

import httpx
import pytest

from app import fetch, gdrive, jobs, worker
from fakes import connect

URL = 'https://www.airbnb.co.uk/rooms/12345?adults=2'
SUCCESS = r'''
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
(d / 'reel.mp4').write_bytes(b'primary-video'); (d / 'reel-720p.mp4').write_bytes(b'small-video')
print(json.dumps({'log': 'Fetching listing 12345 from /Users/someone/secret/path'}), flush=True)
print(json.dumps({'log': 'Rendering cinematic 16:9 walkthrough (v2)'}), flush=True)
print(json.dumps({'result': {'video': str(d / 'reel.mp4'), 'video_720': str(d / 'reel-720p.mp4'), 'duration': 30.0,
      'listing': {'url': 'https://www.airbnb.co.uk/rooms/12345', 'title': 'Flat', 'city': 'Leeds'},
      'ai_plan': {'shots': [{'move': 'DOLLY', 'error': 'HTTPError https://provider.example/signed?token=abc'}]}}}), flush=True)
'''
# The real child's result pipe closes when render_child returns, while the interpreter (torch,
# worker threads) is still shutting down: a finished render must not be killed during that exit.
SLOW_EXIT = SUCCESS + r'''
import os, time
sys.stdout.flush(); os.close(1)
time.sleep(1.5)
'''
FAILURE = r'''
import json, pathlib, sys
(pathlib.Path(sys.argv[1]) / 'partial.mp4').write_bytes(b'half')
print(json.dumps({'error': 'Only 3 usable photos found on that page.'}), flush=True)
sys.exit(1)
'''
HANG = r'''
import json, pathlib, subprocess, sys, time
d = pathlib.Path(sys.argv[1])
g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
(d.parent / 'grandchild.pid').write_text(str(g.pid))
print(json.dumps({'log': 'Rendering'}), flush=True)
time.sleep(120)
'''


def command(script):
    return lambda job, d: [sys.executable, '-c', script, str(d)]


@pytest.fixture
def env(owners, google, monkeypatch, tmp_path):
    monkeypatch.setenv('RENDER_TMP_DIR', str(tmp_path / 'scratch'))
    monkeypatch.setattr(worker, 'LEASE', 5)
    monkeypatch.setattr(worker, 'BEAT', 0.2)
    worker._stop.clear()
    connect(owners, google)
    return owners


def usage_rows(db):
    with db.connect() as c:
        return c.execute('SELECT job_id,refunded_at FROM usage').fetchall()


def claimed(db):
    job = jobs.claim('test-worker', 5)
    assert job and job['owner_email'] == 'alice@example.test'
    return job


def test_missing_drive_rejected_before_any_charge(owners, google, db):
    with pytest.raises(jobs.AdmissionError) as err:
        jobs.admit('bob@example.test', URL, {'attested': True})
    assert err.value.status == 412
    assert usage_rows(db) == []
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'] == 0


def test_admission_is_idempotent_and_owner_scoped(env, db):
    a = jobs.admit('alice@example.test', URL, {'attested': True, 'style': 'cinematic'}, 'key-1')
    assert jobs.admit('alice@example.test', URL, {'attested': True, 'style': 'cinematic'}, 'key-1')['id'] == a['id']
    with pytest.raises(jobs.AdmissionError) as err:
        jobs.admit('alice@example.test', URL, {'attested': True, 'style': 'tutorial'}, 'key-1')
    assert err.value.status == 409
    assert len(usage_rows(db)) == 1
    assert a['url'] == 'https://www.airbnb.co.uk/rooms/12345' and a['params']['max_seconds'] == 60
    assert jobs.get('bob@example.test', a['id']) is None and jobs.list_for('bob@example.test') == []
    assert jobs.cancel('bob@example.test', a['id']) is None


def test_concurrent_same_key_admits_once(env, db):
    ids, errors = [], []

    def go():
        try:
            ids.append(jobs.admit('alice@example.test', URL, {'attested': True}, 'same')['id'])
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(set(ids)) == 1 and len(usage_rows(db)) == 1


def test_refused_reservation_rolls_back_job(env, db):
    for i in range(2):
        jobs.admit('alice@example.test', f'https://www.airbnb.co.uk/rooms/{i + 1}', {'attested': True}, f'k{i}')
    with pytest.raises(jobs.AdmissionError) as err:
        jobs.admit('alice@example.test', 'https://www.airbnb.co.uk/rooms/99', {'attested': True}, 'k-over')
    assert err.value.status == 402
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'] == 2


@pytest.mark.parametrize('bad', ['http://169.254.169.254/rooms/1', 'file:///etc/rooms/1', 'https://evil.test/rooms/1',
                                 'https://www.airbnb.co.uk.evil.test/rooms/1'])
def test_only_airbnb_listing_links_admitted(env, bad):
    with pytest.raises(jobs.AdmissionError):
        jobs.admit('alice@example.test', bad, {'attested': True})


def test_claims_are_exclusive(env, db):
    jobs.admit('alice@example.test', URL, {'attested': True})
    got = []
    threads = [threading.Thread(target=lambda: got.append(jobs.claim('w', 5))) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([g for g in got if g]) == 1


def test_stale_running_job_fails_refunds_and_is_never_rerun(env, db):
    stale = jobs.admit('alice@example.test', URL, {'attested': True}, 'a')
    queued = jobs.admit('alice@example.test', 'https://www.airbnb.co.uk/rooms/777', {'attested': True}, 'b')
    job = jobs.claim('crashed', 5)
    assert job['id'] == stale['id']
    with db.connect() as c:
        c.execute('UPDATE jobs SET lease_until=%s WHERE id=%s', (time.time() - 1, stale['id']))
    assert jobs.recover_stale() == [stale['id']]
    after = jobs.get('alice@example.test', stale['id'])
    assert after['status'] == 'failed' and 'not retried' in after['error']
    assert not jobs.report(job['id'], job['lease_token'], line='late report')
    assert not jobs.finish(job['id'], job['lease_token'], 'done')
    assert {r['job_id']: r['refunded_at'] is not None for r in usage_rows(db)} == {stale['id']: True, queued['id']: False}
    assert jobs.get('alice@example.test', queued['id'])['status'] == 'queued'


def test_worker_success_delivers_every_variant_privately_and_cleans(env, google, db):
    job = jobs.admit('alice@example.test', URL, {'attested': True, 'ai_resolution': '720p'})
    worker.process(claimed(db), command(SUCCESS))
    done = jobs.get('alice@example.test', job['id'])
    assert done['status'] == 'done' and done['progress'] == 100 and done['cleanup_at']
    for variant in ('primary', '720p'):
        rec = gdrive.receipt('alice@example.test', job['id'], variant)
        assert rec['confirmed'] and rec['sharing'] == 'private'
    assert not worker.scratch(job['id']).exists()
    stored = str(done['log']) + str(done['meta'])
    assert '/Users/' not in stored and 'token=abc' not in stored and 'provider.example' not in stored
    assert not any('/permissions' in r.url.path for r in google.calls)


def test_worker_failure_refunds_sanitizes_and_cleans(env, db):
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    worker.process(claimed(db), command(FAILURE))
    failed = jobs.get('alice@example.test', job['id'])
    assert failed['status'] == 'failed' and failed['error'] == 'Only 3 usable photos found on that page.'
    assert usage_rows(db)[0]['refunded_at'] is not None
    assert not worker.scratch(job['id']).exists() and failed['cleanup_at']


def test_render_that_exits_slowly_after_its_result_is_delivered(env, google, db):
    job = jobs.admit('alice@example.test', URL, {'attested': True, 'ai_resolution': '720p'})
    worker.process(claimed(db), command(SLOW_EXIT))
    done = jobs.get('alice@example.test', job['id'])
    assert done['status'] == 'done', done['error']
    assert gdrive.receipt('alice@example.test', job['id'], 'primary')['confirmed']


def test_incomplete_upload_is_never_done(env, google, db):
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    google.bad_receipt = True
    worker.process(claimed(db), command(SUCCESS))
    failed = jobs.get('alice@example.test', job['id'])
    assert failed['status'] == 'failed' and gdrive.receipt('alice@example.test', job['id']) is None
    assert not worker.scratch(job['id']).exists()


def test_reconnect_after_admission_never_delivers_elsewhere(env, google, db):
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    google.sub = 'someone-else'
    connect(env, google)
    worker.process(claimed(db), command(SUCCESS))
    failed = jobs.get('alice@example.test', job['id'])
    assert failed['status'] == 'failed' and 'reconnected' in failed['error']
    assert gdrive.receipt('alice@example.test', job['id']) is None


def _dead(pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_cancel_kills_render_process_group_and_cleans(env, db, tmp_path):
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    running = claimed(db)
    t = threading.Thread(target=worker.process, args=(running, command(HANG)))
    t.start()
    pid_file = tmp_path / 'scratch' / 'grandchild.pid'
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        time.sleep(0.1)
    assert jobs.cancel('alice@example.test', job['id'])['cancel_requested']
    t.join(30)
    assert not t.is_alive()
    after = jobs.get('alice@example.test', job['id'])
    assert after['status'] == 'cancelled' and usage_rows(db)[0]['refunded_at'] is not None
    assert _dead(int(pid_file.read_text()))
    assert not worker.scratch(job['id']).exists()


def test_cancel_queued_job_refunds_immediately(env, db):
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    assert jobs.cancel('alice@example.test', job['id'])['status'] == 'cancelled'
    assert usage_rows(db)[0]['refunded_at'] is not None and jobs.claim('w', 5) is None


def test_sweeper_skips_live_leases(env, db, tmp_path):
    live = jobs.admit('alice@example.test', URL, {'attested': True})
    claimed(db)
    base = tmp_path / 'scratch'
    (base / f"job-{live['id']}").mkdir(parents=True)
    (base / 'job-deadbeef00').mkdir()
    worker.sweep()
    assert (base / f"job-{live['id']}").exists() and not (base / 'job-deadbeef00').exists()


def test_resolution_is_per_job_not_process_environment(env, db, monkeypatch, tmp_path):
    monkeypatch.delenv('AI_RESOLUTION', raising=False)
    jobs.admit('alice@example.test', URL, {'attested': True, 'ai_resolution': '720p', 'ai_motion': True})
    cmd = worker.render_command(claimed(db), tmp_path)
    assert '"ai_resolution": "720p"' in cmd[4] and '"ai_motion": false' in cmd[4]  # free plan cannot buy AI motion
    assert 'AI_RESOLUTION' not in os.environ


@pytest.mark.parametrize('bad', ['http://127.0.0.1/', 'http://169.254.169.254/latest/meta-data', 'file:///etc/passwd',
                                 'http://93.184.216.34:8080/', 'http://[::1]/', 'gopher://93.184.216.34/'])
def test_fetch_rejects_non_public_targets(bad):
    with pytest.raises(ValueError):
        fetch.check(bad)


def test_fetch_rechecks_redirect_targets(monkeypatch):
    def handler(req):
        return httpx.Response(302, headers={'Location': 'http://169.254.169.254/latest/meta-data'})
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError, match='public'):
        fetch.get('http://93.184.216.34/listing')


def test_cancel_is_refused_once_delivery_has_started(env, db):
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    running = claimed(db)
    assert jobs.start_upload(running['id'], running['lease_token'])
    after = jobs.cancel('alice@example.test', job['id'])
    assert after['status'] == 'uploading' and not after['cancel_requested']


def test_cancel_just_before_delivery_wins(env, db):
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    running = claimed(db)
    jobs.cancel('alice@example.test', job['id'])
    assert not jobs.start_upload(running['id'], running['lease_token'])
    worker.process(running, command(SUCCESS))
    after = jobs.get('alice@example.test', job['id'])
    assert after['status'] == 'cancelled' and gdrive.receipt('alice@example.test', job['id']) is None


def test_upload_stops_between_chunks_when_told(env, tmp_path):
    path = tmp_path / 'reel.mp4'; path.write_bytes(b'x' * (256 * 1024 * 3))
    calls = []
    with pytest.raises(RuntimeError, match='stopped'):
        gdrive.upload(path, 'listing', 'alice@example.test', job_id='abort', chunk=256 * 1024,
                      keep_going=lambda: calls.append(1) or len(calls) < 2)
    assert gdrive.receipt('alice@example.test', 'abort') is None


def test_shutdown_stops_render_promptly_even_between_heartbeats(env, db, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'BEAT', 30)
    job = jobs.admit('alice@example.test', URL, {'attested': True})
    running = claimed(db)
    t = threading.Thread(target=worker.process, args=(running, command(HANG)))
    t.start()
    pid_file = tmp_path / 'scratch' / 'grandchild.pid'
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        time.sleep(0.1)
    began = time.time()
    worker._stop.set()
    t.join(20)
    worker._stop.clear()
    assert not t.is_alive() and time.time() - began < 12
    assert jobs.get('alice@example.test', job['id'])['status'] == 'failed'
    assert _dead(int(pid_file.read_text()))
