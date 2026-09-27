"""Render worker: claims durable jobs, renders each in its own process group inside disposable
scratch space, delivers every output to the owner's Drive, then deletes the scratch.

Run: python -m app.worker            (the loop)
     python -m app.worker render ... (internal: one render child, JSON lines on stdout)

Nothing customer-facing is persisted on this machine: scratch lives under RENDER_TMP_DIR, is
removed in `finally`, and a sweeper removes anything a crash left behind (never a directory
whose job still holds a live lease). Cleanup is recorded only after the directory is verified gone.
"""
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from app import companies, gdrive, invoices, jobs, photos, retention, store

LEASE = int(os.getenv('WORKER_LEASE_SECONDS', '90'))
BEAT = max(1.0, LEASE / 6)
EXIT_GRACE = 30  # seconds a finished child may take to exit (torch teardown) before it is killed
# Log-line prefixes in pipeline order; progress only ever rises (jobs.report keeps the max), so a later
# "Estimating depth for N more frames" line leaves the bar where it is.
STEPS = ['Fetching', 'Captured', 'Downloaded', 'Scoring', 'Estimating depth', 'Scored', 'Audit', 'AI motion plan',
         'Seedance', 'Rendering', 'Rendered', 'Uploading']
VARIANTS = (('primary', 'video'), ('720p', 'video_720'))
PHOTO_FACTS = ('title', 'location', 'highlights', 'quotes', 'rooms')
_stop = threading.Event()
_last_purge = [0.0]


class Stopped(Exception):
    """The job must stop: cancelled by its owner, lease lost, or the worker is shutting down."""


def root():
    return Path(os.getenv('RENDER_TMP_DIR') or '/tmp/reelsieve')


def scratch(job_id):
    if not jobs.JOB_ID.match(job_id or ''):
        raise ValueError('bad job id')
    return root() / f'job-{job_id}'


def _remove(path):
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def sweep():
    """Bounded cleanup of scratch left by crashes or failed deletes; skips live leases."""
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    live = jobs.live_leases()
    for d in list(base.iterdir())[:200]:
        if d.name.startswith('job-') and d.name[4:] not in live:
            _remove(d)
    for job in jobs.pending_cleanup():
        if job['id'] not in live:
            _drop_inputs(job)  # e.g. a photo reel cancelled before any worker claimed it
            jobs.mark_cleaned(job['id'], None if _remove(scratch(job['id'])) else 'scratch directory could not be deleted')


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill(proc):
    """Terminate the whole render process group (renderer, ffmpeg, depth model), escalating if ignored."""
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        proc.poll()  # reap our own child so a zombie does not keep the group "alive"
        if not _group_alive(proc.pid):
            break
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        end = time.time() + grace
        while time.time() < end and _group_alive(proc.pid):
            proc.poll()
            time.sleep(0.1)
    proc.wait()


def _pump(stream, lines):
    for raw in stream:
        lines.put(raw)
    lines.put(None)


def run_child(cmd, job, on_line):
    """Run one render child; heartbeat while it runs; stop it (and its group) when told to."""
    # OpenCV decodes up to 2^30 pixels by default; nothing a reel uses is near the upload limit (defence in depth).
    env = {**os.environ, 'OPENCV_IO_MAX_IMAGE_PIXELS': str(photos.MAX_PIXELS)}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            start_new_session=True, text=True, env=env)
    lines = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()
    result, error, next_beat, eof_at = None, None, 0.0, None
    try:
        while True:
            if _stop.is_set():
                raise Stopped()
            if time.time() >= next_beat:
                if not jobs.heartbeat(job['id'], job['lease_token'], LEASE):
                    raise Stopped()
                next_beat = time.time() + BEAT
            if eof_at is not None:
                # The result pipe closes before the child's interpreter has finished exiting.
                if proc.poll() is not None or time.time() - eof_at > EXIT_GRACE:
                    break
                time.sleep(0.1)
                continue
            try:
                raw = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if raw is None:
                eof_at = time.time()
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if 'log' in msg:
                on_line(msg['log'])
            elif 'result' in msg:
                result = msg['result']
            elif 'error' in msg:
                error = msg['error']
    finally:
        _kill(proc)
    if proc.returncode != 0 or result is None:
        raise RuntimeError(error or 'Rendering stopped unexpectedly')
    return result


def render_command(job, workdir):
    p = job['params']
    own = p.get('source') == 'photos'  # the customer's own photos, fetched into scratch by process(): no link at all
    spec = {'ai_motion': bool(p.get('ai_motion')) and not own, 'renderer': p.get('style', 'v2'),  # never Drive data to Higgsfield
            'max_seconds': p.get('max_seconds'), 'ai_resolution': p.get('ai_resolution', '1080p')}
    if own:
        spec.update(photos=str(Path(workdir) / 'inputs'), facts={k: p.get(k) for k in PHOTO_FACTS})
    else:
        spec['url'] = job['url']
    return [sys.executable, '-m', 'app.worker', 'render', json.dumps(spec), str(workdir)]


def _keep_going(lost):
    return not lost.is_set() and not _stop.is_set()


def _fetch_inputs(job, workdir):
    """A photo reel's inputs, from the owner's Drive (pinned to the admitted connection) into scratch."""
    jobs.report(job['id'], job['lease_token'], step='Fetching your photos from Google Drive', progress=6,
                line='Fetching your photos from Google Drive')
    stop, lost = threading.Event(), threading.Event()
    threading.Thread(target=_beating, args=(job, stop, lost), daemon=True).start()
    try:
        gdrive.download_inputs(job['owner_email'], job['params']['photos']['ids'], Path(workdir) / 'inputs',
                               job['drive_generation'], keep_going=lambda: _keep_going(lost))
    except RuntimeError:
        if _keep_going(lost):
            raise  # a real failure; otherwise the owner cancelled or the worker is stopping
    finally:
        stop.set()
    if not _keep_going(lost):
        raise Stopped()


def _drop_inputs(job):
    """The customer asked for their uploaded photos to be deleted from their Drive once the reel is over."""
    jobs.drop_inputs(job)  # current connection, never raises; a failure is retried hourly by app.retention


def _progress(job, line):
    pct = None
    for i, s in enumerate(STEPS):
        if line.startswith(s):
            pct = 8 + i * 8
    jobs.report(job['id'], job['lease_token'], step=line, progress=pct, line=line)


def _beating(job, stop, lost):
    while not stop.wait(BEAT):
        if not jobs.heartbeat(job['id'], job['lease_token'], LEASE):
            lost.set()
            return


def deliver(job, result):
    """Upload every promised output to the owner's Drive, pinned to the connection the job was admitted on."""
    token = job['lease_token']
    if not jobs.start_upload(job['id'], token):
        raise Stopped()
    jobs.report(job['id'], token, line='Uploading to your Google Drive')
    listing = result.get('listing') or {}
    description = f"{listing.get('title') or ''} · {listing.get('city') or ''} · Listing Reel by Braivex"
    name = listing.get('url') or listing.get('title') or job['url']  # own-photo reels have no link: named by title
    stop, lost = threading.Event(), threading.Event()
    beat = threading.Thread(target=_beating, args=(job, stop, lost), daemon=True)
    beat.start()
    # ponytail: a lost lease or shutdown stops the upload between chunks; a chunk already in flight
    # still completes. With several worker replicas a very slow chunk could outlive the lease.
    keep_going = lambda: not lost.is_set() and not _stop.is_set()  # noqa: E731
    try:
        for variant, key in VARIANTS:
            if result.get(key):
                gdrive.upload(result[key], name, job['owner_email'], description=description,
                              job_id=job['id'], variant=variant, generation=job['drive_generation'], keep_going=keep_going)
    finally:
        stop.set()
    promised = [v for v, k in VARIANTS if result.get(k)]
    missing = [v for v in promised if not gdrive.receipt(job['owner_email'], job['id'], v)]
    if not promised or missing:
        raise RuntimeError('Google Drive did not confirm every file — nothing is marked delivered')


def _safe_result(result):
    """What the job page needs, without local paths or raw provider errors."""
    plan = result.get('ai_plan')
    if plan:
        for shot in plan.get('shots') or []:
            if shot.get('error'):
                shot['error'] = jobs.clean(shot['error'], 120)
    return {'listing': result.get('listing') or {}, 'duration': result.get('duration'), 'audit': result.get('audit'),
            'ai_plan': plan, 'selection': result.get('selection'), 'photo_scores': result.get('photo_scores')}


def _refuse_if_removed(job):
    """A host's takedown also stops a reel queued or rendering when it lands: checked before the render and again
    before delivery. Fails the job, which refunds it."""
    if store.blocked_ids([jobs.listing_id(job['url'])]):
        raise RuntimeError(jobs.REMOVED)


def process(job, command=render_command):
    d = scratch(job['id'])
    token = job['lease_token']
    try:
        _remove(d)
        d.mkdir(parents=True)
        _refuse_if_removed(job)
        if (job['params'] or {}).get('source') == 'photos':
            _fetch_inputs(job, d)
        result = run_child(command(job, d), job, lambda line: _progress(job, line))
        _refuse_if_removed(job)
        jobs.report(job['id'], token, meta=_safe_result(result), line='Reel ready')
        deliver(job, result)
        jobs.report(job['id'], token, line='Delivered to your Google Drive')
        jobs.finish(job['id'], token, 'done')
    except Stopped:
        if jobs.cancel_requested(job['id']):
            jobs.finish(job['id'], token, 'cancelled')
        elif _stop.is_set():
            jobs.finish(job['id'], token, 'failed', 'The server restarted while this reel was being made. It was not '
                                                    'retried automatically and nothing was charged — start it again.')
    except Exception as e:  # anything else is a failed job with a sanitized reason
        jobs.finish(job['id'], token, 'failed', str(e) if isinstance(e, RuntimeError) else 'Rendering failed')
    finally:
        _drop_inputs(job)
        jobs.mark_cleaned(job['id'], None if _remove(d) else 'scratch directory could not be deleted')


_refreshing = threading.Lock()


def _refresh_companies():
    """A monthly Companies House load takes minutes, so it runs beside job claims, one at a time in this process
    (and on one replica: an advisory lock). refresh never raises. A load cut off by shutdown rolls back."""
    if _refreshing.acquire(blocking=False):
        try:
            companies.refresh()
        finally:
            _refreshing.release()


def run_once(worker, command=render_command):
    jobs.recover_stale()
    sweep()
    if time.time() - _last_purge[0] > 3600:
        _last_purge[0] = time.time()  # first: a failing purge must never stop jobs being claimed; it retries next hour
        threading.Thread(target=_refresh_companies, name='companies-refresh', daemon=True).start()
        for task in (store.purge_signals, retention.run, invoices.backup):
            try:  # one failing task never skips the others (the India backup is a legal duty)
                task()
            except Exception as e:
                print(json.dumps({'hourly_task_failed': task.__name__, 'error': type(e).__name__}), flush=True)
    job = jobs.claim(worker, LEASE)
    if not job:
        return False
    process(job, command)
    return True


def main():
    worker = f'{socket.gethostname()}-{os.getpid()}'
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    while not _stop.is_set():
        try:
            if not run_once(worker):
                _stop.wait(3)
        except Exception:  # database blip: back off, never crash-loop through a job
            _stop.wait(10)


def render_child(spec, workdir):
    """Child side: JSON protocol on the original stdout; everything else printed goes to stderr."""
    proto = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    emit = lambda **kw: proto.write(json.dumps(kw) + '\n')  # noqa: E731
    try:
        from app import pipeline
        spec = json.loads(spec)
        cb = lambda m: emit(log=jobs.clean(m))  # noqa: E731
        if spec.get('photos'):
            res = pipeline.run_photos(spec['photos'], spec['facts'], workdir, spec['ai_motion'], cb, spec['renderer'],
                                      spec['max_seconds'], spec['ai_resolution'])
        else:
            res = pipeline.run(spec['url'], workdir, None, spec['ai_motion'], cb, None,
                               spec['renderer'], spec['max_seconds'], ai_resolution=spec['ai_resolution'])
        emit(result=res)
    except Exception as e:
        emit(error=jobs.clean(str(e), 300) if isinstance(e, (RuntimeError, ValueError)) else 'Rendering failed')
        sys.exit(1)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == 'render':
        render_child(sys.argv[2], sys.argv[3])
    else:
        main()
