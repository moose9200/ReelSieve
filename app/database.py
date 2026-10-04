"""PostgreSQL connection boundary. Importing this module never opens a database.

Connections come from a per-process pool (DB_POOL_MAX, default 10) created on first use. A
connection found dead on its first statement (database restart) is discarded and another taken,
so a restart costs a reconnect, not an error. Each `with connect()` block is one transaction: commit on exit, rollback on error.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import threading
import time

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pools = {}
_pools_lock = threading.Lock()


def schema_name():
    name = os.getenv('DATABASE_SCHEMA', 'public')
    if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', name):
        raise RuntimeError('DATABASE_SCHEMA must be a valid PostgreSQL schema identifier')
    if name.startswith('pg_') or name == 'information_schema':
        raise RuntimeError('DATABASE_SCHEMA cannot be a system schema')
    return name


@contextmanager
def connect():
    dsn = os.getenv('DATABASE_URL', '')
    if not dsn:
        raise RuntimeError('DATABASE_URL is required')
    if not dsn.startswith(('postgresql://', 'postgres://')):
        raise RuntimeError('DATABASE_URL must use PostgreSQL')
    search_path = sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema_name()))
    pool = _pool(dsn)
    for attempt in range(1, _pool_size(pool) + 2):
        conn = pool.getconn(timeout=30)
        try:
            conn.execute(search_path)
            break
        except psycopg.OperationalError:
            # A pooled connection died (database restart, idle drop): discard it and take another.
            pool.putconn(conn)
            if attempt > _pool_size(pool):
                raise
    try:
        yield conn
        conn.commit()
    except BaseException:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _pool_size(pool):
    return pool.get_stats().get('pool_size', 1)


def _pool(dsn):
    with _pools_lock:
        pool = _pools.get(dsn)
        if pool is None:
            # Explicit UTF-8 also makes TEXT values strings on SQL_ASCII test clusters.
            pool = ConnectionPool(dsn, min_size=1, max_size=int(os.getenv('DB_POOL_MAX', '10')), name='reelsieve',
                                  kwargs={'row_factory': dict_row, 'client_encoding': 'UTF8'}, open=True)
            _pools[dsn] = pool
        return pool


@contextmanager
def transaction(conn=None):
    """Use the caller's transaction without committing it, or own a new one."""
    if conn is not None:
        yield conn
    else:
        with connect() as owned:
            yield owned


SCHEMA = Path(__file__).parent / 'schema'
LEDGER_FROM = '012'  # files from 012 on run once and are recorded; earlier files are idempotent and re-run every start


def initialize():
    """Apply the schema in one transaction. Only the web service calls this (app/start.py, the web lifespan); a worker
    checks missing_schema() instead. Files before 012 are idempotent and re-run at every start. From 012 each file runs
    once and its sha256 goes in schema_ledger, so destructive SQL never runs twice, and a file edited after it ran
    stops the start instead of being skipped silently."""
    with connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('reelsieve-schema-' + schema_name(),))
        conn.execute('CREATE TABLE IF NOT EXISTS schema_ledger (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, '
                     'applied_at DOUBLE PRECISION NOT NULL)')  # the runner's own bookkeeping
        for path in sorted(SCHEMA.glob('*.sql')):
            text = path.read_bytes()
            if path.name < LEDGER_FROM:
                conn.execute(text.decode())
                continue
            digest = hashlib.sha256(text).hexdigest()
            row = conn.execute('SELECT sha256 FROM schema_ledger WHERE name=%s', (path.name,)).fetchone()
            if row is None:
                conn.execute(text.decode())
                conn.execute('INSERT INTO schema_ledger(name,sha256,applied_at) VALUES(%s,%s,%s)', (path.name, digest, time.time()))
            elif row['sha256'] != digest:
                raise RuntimeError(f'{path.name} changed after it was applied: put the change in a new schema file')


def missing_schema():
    """The run-once schema files this code expects that the database has not applied yet (all of them before the
    web service's first start with a ledger)."""
    names = sorted(p.name for p in SCHEMA.glob('*.sql') if p.name >= LEDGER_FROM)
    try:
        with connect() as conn:
            done = {r['name'] for r in conn.execute('SELECT name FROM schema_ledger').fetchall()}
    except psycopg.errors.UndefinedTable:
        done = set()
    return [n for n in names if n not in done]


def user_id(email, conn=None):
    with transaction(conn) as c:
        row = c.execute('SELECT id FROM users WHERE email=%s', ((email or '').strip().lower(),)).fetchone()
        if not row:
            raise ValueError('No such user')
        return row['id']
