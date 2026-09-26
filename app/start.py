"""Container entry point: python -m app.start

1. Refuse to start without DATABASE_URL, SESSION_SECRET and a valid TOKEN_ENCRYPTION_KEY.
2. Apply the schema.
3. With LEGACY_MIGRATION_ENABLED=1 only: import LEGACY_SOURCE_DIR once (a completion marker
   makes later starts a no-op; a changed source or any ownership doubt stops the start).
4. Run the web process and, with WORKER_ENABLED=1 (default), the render worker. SIGTERM/SIGINT
   are passed on; if either child exits the other is stopped and the container exits non-zero.
   As PID 1 it also reaps orphaned render processes.
"""
import json
import os
import signal
import subprocess
import sys
import time

from app import database, migrate_cloud


def commands():
    web = [sys.executable, '-m', 'uvicorn', 'app.server:app', '--host', '0.0.0.0', '--port', os.getenv('PORT', '8787')]
    worker = [sys.executable, '-m', 'app.worker']
    return [web] + ([worker] if os.getenv('WORKER_ENABLED', '1') == '1' else [])


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


def main():
    from app import server
    server.validate_config()
    database.initialize()
    if os.getenv('LEGACY_MIGRATION_ENABLED') == '1':
        source = os.getenv('LEGACY_SOURCE_DIR')
        if not source:
            sys.exit('LEGACY_MIGRATION_ENABLED=1 needs LEGACY_SOURCE_DIR')
        try:
            print(json.dumps({'legacy_migration': migrate_cloud.run(source, apply=True)}, sort_keys=True), flush=True)
        except migrate_cloud.MigrationError as e:
            sys.exit(str(e))
    sys.exit(supervise(commands()))


if __name__ == '__main__':
    main()
