"""Legacy volume archive: encrypted in PostgreSQL, idempotent, restorable."""
import pytest
from cryptography.fernet import Fernet

from app import archive_legacy


@pytest.fixture
def volume(tmp_path, db, monkeypatch):
    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    root = tmp_path / 'data'
    (root / 'jobs' / 'abcdef1234').mkdir(parents=True)
    (root / 'hf-cache').mkdir()
    (root / 'auth.json').write_text('{"users": {"a@x.test": {"hash": "plaintext-marker-hash"}}}')
    (root / 'jobs' / 'abcdef1234' / 'job.json').write_text('{"id": "abcdef1234"}')
    (root / 'hf-cache' / 'model.bin').write_bytes(b'big model')
    return root


def test_archive_is_encrypted_idempotent_and_restorable(volume, db, tmp_path):
    out = archive_legacy.archive(volume)
    assert out['status'] == 'archived' and out['files'] == 2  # model cache skipped
    with db.connect() as c:
        stored = bytes(c.execute('SELECT data FROM legacy_archives').fetchone()['data'])
    assert b'plaintext-marker-hash' not in stored
    assert archive_legacy.archive(volume)['status'] == 'already-archived'
    restored = tmp_path / 'restored'
    assert archive_legacy.restore(restored)['files'] == 2
    assert (restored / 'auth.json').read_text() == (volume / 'auth.json').read_text()
    assert (restored / 'jobs' / 'abcdef1234' / 'job.json').exists() and not (restored / 'hf-cache').exists()


def test_changed_source_is_never_overwritten(volume):
    archive_legacy.archive(volume)
    (volume / 'auth.json').write_text('{}')
    with pytest.raises(archive_legacy.ArchiveError, match='refusing'):
        archive_legacy.archive(volume)
