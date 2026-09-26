"""Data-protection rights and minimisation (UK GDPR, EU GDPR, India DPDP) against real isolated PostgreSQL.
Google is the synthetic fake; nothing leaves the machine."""
import time

from fastapi.testclient import TestClient

from app import auth, server

ALICE, BOB, ADMIN = 'alice@example.test', 'bob@example.test', 'operator@example.test'


def client_for(session=None):
    c = TestClient(server.app)
    c.__enter__()
    if session:
        c.cookies.set(auth.COOKIE, session)
    return c


def csrf(session):
    return {'X-CSRF-Token': auth.csrf_token(session)}


# ---------------- 10. password hashing (Art 32) ----------------

def test_new_password_hashes_use_the_owasp_pbkdf2_sha256_work_factor(db):
    # OWASP Password Storage Cheat Sheet, fetched 26 Sep 2026: "PBKDF2-HMAC-SHA256: 600,000 iterations (recommended)"
    auth.create_user(ALICE, 'long-initial-password')
    with db.connect() as c:
        assert c.execute('SELECT iterations FROM users').fetchone()['iterations'] == 600_000
    auth.set_password(ALICE, 'another-long-password')
    with db.connect() as c:
        assert c.execute('SELECT iterations FROM users').fetchone()['iterations'] == 600_000


def test_old_hash_still_signs_in_and_is_upgraded_without_signing_anyone_out(db):
    salt = '02' * 16
    with db.connect() as c:
        c.execute('INSERT INTO users(id,email,salt,hash,iterations,role,created) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                  ('old-owner', ALICE, salt, auth._hash('old-password-1', salt, 200_000), 200_000, 'member', time.time()))
    cookie, _ = auth.issue(ALICE)
    assert not auth.verify(ALICE, 'wrong-password')
    with db.connect() as c:
        assert c.execute('SELECT iterations FROM users').fetchone()['iterations'] == 200_000  # a failed attempt changes nothing
    assert auth.verify(ALICE, 'old-password-1')
    with db.connect() as c:
        row = c.execute('SELECT salt,iterations FROM users').fetchone()
    assert row['iterations'] == 600_000 and row['salt'] != salt
    assert auth.verify(ALICE, 'old-password-1') and auth.check(cookie) == ALICE
