"""PostgreSQL connection boundary. Importing this module never opens a database."""
from contextlib import contextmanager
import os
from pathlib import Path
import re

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


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
    schema = schema_name()
    # Explicit UTF-8 also makes TEXT values strings on SQL_ASCII test clusters.
    with psycopg.connect(dsn, row_factory=dict_row, client_encoding='UTF8') as conn:
        conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
        yield conn


@contextmanager
def transaction(conn=None):
    """Use the caller's transaction without committing it, or own a new one."""
    if conn is not None:
        yield conn
    else:
        with connect() as owned:
            yield owned


def initialize():
    """Run ordered, idempotent schema files explicitly at application startup."""
    with connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('reelsieve-schema-' + schema_name(),))
        for path in sorted((Path(__file__).parent / 'schema').glob('*.sql')):
            conn.execute(path.read_text())


def user_id(email, conn=None):
    with transaction(conn) as c:
        row = c.execute('SELECT id FROM users WHERE email=%s', ((email or '').strip().lower(),)).fetchone()
        if not row:
            raise ValueError('No such user')
        return row['id']
