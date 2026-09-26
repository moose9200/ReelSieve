"""Disposable PostgreSQL schemas only; never load the application's local environment."""
import os
import uuid

import httpx
import psycopg
from psycopg import sql
import pytest
from cryptography.fernet import Fernet

from fakes import Google


@pytest.fixture
def db(monkeypatch, tmp_path):
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL is required for isolated PostgreSQL tests')
    schema = 'test_' + uuid.uuid4().hex
    monkeypatch.setenv('DATABASE_URL', dsn)
    monkeypatch.setenv('DATABASE_SCHEMA', schema)
    monkeypatch.setenv('SESSION_SECRET', 'synthetic-test-session-secret-only')
    # Protect against legacy modules during the initial red test run.
    monkeypatch.setenv('AUTH_PATH', str(tmp_path / 'synthetic-auth.json'))
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'synthetic-store.db'))
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        from app import database
        database.initialize()
        yield database
    finally:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def owners(db, monkeypatch):
    from app import auth
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'synthetic-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'synthetic-secret')
    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    for name in ('alice', 'bob'):
        auth.create_user(name + '@example.test', 'synthetic-password')
    return {name: auth.issue(name + '@example.test')[0] for name in ('alice', 'bob')}


@pytest.fixture
def google(monkeypatch):
    fake = Google()
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(fake.handle), **kw))
    return fake
