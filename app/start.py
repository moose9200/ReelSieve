"""Container entry point: python -m app.start

1. Refuse to start without DATABASE_URL, SESSION_SECRET and a valid TOKEN_ENCRYPTION_KEY.
2. Apply the schema.
3. With LEGACY_MIGRATION_ENABLED=1 only: import LEGACY_SOURCE_DIR once (a completion marker
   makes later starts a no-op; a changed source or any ownership doubt stops the start).
4. Run the web process (WEB_ENABLED=1, default) and/or the render worker (WORKER_ENABLED=1,
   default). A worker-only service (WEB_ENABLED=0) still answers /healthz for the platform.
   SIGTERM/SIGINT are passed on; if a child exits the others are stopped and the container
   exits non-zero. As PID 1 it also reaps orphaned render processes.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import subprocess
import sys
import threading
import time

from app import database, migrate_cloud


def commands():
    # No access log: it would print client addresses and full query strings (OAuth code/state, Stripe session id).
    web = [sys.executable, '-m', 'uvicorn', 'app.server:app', '--host', '0.0.0.0', '--port', os.getenv('PORT', '8787'),
           '--no-access-log']
    worker = [sys.executable, '-m', 'app.worker']
    cmds = ([web] if os.getenv('WEB_ENABLED', '1') == '1' else []) + ([worker] if os.getenv('WORKER_ENABLED', '1') == '1' else [])
    if not cmds:
        raise SystemExit('Nothing to run: set WEB_ENABLED and/or WORKER_ENABLED to 1')
    return cmds


def health_server(port, build):
    """For worker-only containers: /healthz reports whether the database is reachable."""
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                with database.connect() as c:
                    c.execute('SELECT 1')
                db = True
            except Exception:
                db = False
            ok = db and self.path == '/healthz'
            body = json.dumps({'ok': ok, 'role': 'worker', 'build': build, 'db': db}).encode()
            self.send_response(200 if ok else 503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('0.0.0.0', port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _reap(procs):
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return
        for p in procs:
            if p.pid == pid and p.returncode is None:
                p.returncode = os.waitstatus_to_exitcode(status)


def supervise(cmds, grace=25.0):
    procs = [subprocess.Popen(c) for c in cmds]
    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, _f: stop.append(s))
    while not stop and all(p.returncode is None for p in procs):
        _reap(procs)
        time.sleep(0.2)
    crashed = [p for p in procs if p.returncode is not None]
    for p in procs:
        if p.returncode is None:
            p.send_signal(signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline and any(p.returncode is None for p in procs):
        _reap(procs)
        time.sleep(0.2)
    for p in procs:
        if p.returncode is None:
            p.kill()
            p.wait()
    return 1 if crashed and not stop else 0


SCHEMA_WAIT = 120  # ponytail: a worker deployed before the web polls this long, then exits for Railway to restart it


def prepare_schema(web, wait=SCHEMA_WAIT):
    """Only the web service migrates. A worker never does: it waits for the schema this code needs and otherwise exits
    non-zero, so Railway restarts it once the web service has migrated. Deploy the web service first, then the worker."""
    if web:
        database.initialize()
        return
    deadline = time.time() + wait
    while True:
        missing = database.missing_schema()
        if not missing:
            return
        if time.time() >= deadline:
            raise SystemExit('Worker not started: the database is behind this code (not applied: ' + ', '.join(missing)
                             + '). Only the web service migrates: deploy it first, then this worker restarts.')
        time.sleep(5)


def main():
    from app import server
    server.validate_config()
    prepare_schema(os.getenv('WEB_ENABLED', '1') == '1')
    if os.getenv('LEGACY_MIGRATION_ENABLED') == '1':
        source = os.getenv('LEGACY_SOURCE_DIR')
        if not source:
            sys.exit('LEGACY_MIGRATION_ENABLED=1 needs LEGACY_SOURCE_DIR')
        try:
            print(json.dumps({'legacy_migration': migrate_cloud.run(source, apply=True)}, sort_keys=True), flush=True)
        except migrate_cloud.MigrationError as e:
            sys.exit(str(e))
    cmds = commands()
    if os.getenv('WEB_ENABLED', '1') != '1':
        health_server(int(os.getenv('PORT', '8787')), server.BUILD)
    sys.exit(supervise(cmds))


if __name__ == '__main__':
    main()
