"""Disposable PostgreSQL schemas only; never load the application's local environment."""
import functools
import os
import threading
import uuid

import httpx
import psycopg
from psycopg import sql
import pytest
from cryptography.fernet import Fernet

from fakes import Google
from starlette.testclient import TestClient

# Production is HTTPS only and its session and CSRF cookies are __Host- (Secure), which a client sends over HTTPS
# only: tests talk HTTPS unless one passes base_url='http://...' to test plain HTTP on purpose.
TestClient.__init__ = functools.partialmethod(TestClient.__init__, base_url='https://testserver')


@pytest.fixture(autouse=True)
def no_companies_house(monkeypatch):
    """No test ever reaches Companies House: the worker's hourly refresh would otherwise download the real snapshot."""
    from app import companies

    def refuse(*_args, **_kw):
        raise RuntimeError('network disabled in tests')
    monkeypatch.setattr(companies, '_get_page', refuse)
    monkeypatch.setattr(companies, '_download', refuse)
    yield
    for t in threading.enumerate():  # the worker loads snapshots in a thread: let it finish while the network is refused
        if t.name == 'companies-refresh':
            t.join(10)


@pytest.fixture
def db(monkeypatch, tmp_path):
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL is required for isolated PostgreSQL tests')
    schema = 'test_' + uuid.uuid4().hex
    monkeypatch.setenv('DATABASE_URL', dsn)
    monkeypatch.setenv('DATABASE_SCHEMA', schema)
    monkeypatch.setenv('SESSION_SECRET', 'synthetic-test-session-secret-only')
    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())  # the do-not-contact key is stored with it
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
        auth.create_user(name + '@example.test')  # customers have no password: Braivex signs them in
    return {name: auth.issue(name + '@example.test')[0] for name in ('alice', 'bob')}


@pytest.fixture
def google(monkeypatch):
    fake = Google()
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(fake.handle), **kw))
    return fake


@pytest.fixture
def airbnb_net(db, monkeypatch):
    """Synthetic Airbnb pages and photo CDN (never the real site); DNS answers a public address. Pages are paced at
    50/s here so tests stay quick; the 1/s and 10/s defaults and the cross-process sharing have their own tests."""
    import socket
    from fakes import Airbnb
    monkeypatch.setenv('AIRBNB_PAGE_RPS', '50')
    fake = Airbnb()
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(fake.handle), **kw))
    monkeypatch.setattr(socket, 'getaddrinfo',
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))])
    return fake
