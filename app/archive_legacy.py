"""Encrypted copy of the legacy /data volume, kept in PostgreSQL so the volume can be detached.

    python -m app.archive_legacy /data              # archive once; verifies by reading it back
    python -m app.archive_legacy --restore <dir>    # write the files back (rollback only)
    python -m app.archive_legacy --diff <dir>       # paths added, removed or changed since the archive
    python -m app.archive_legacy --purge            # print what it holds (counts), then delete it for good

The archive is a tar.gz of every file except the model cache, encrypted with TOKEN_ENCRYPTION_KEY
(it contains password hashes and Drive tokens). Output is counts and a digest prefix only.
app.retention also deletes it automatically LEGACY_ARCHIVE_KEEP_DAYS (default 90) after it was created.
"""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import time

from app import database, gdrive

NAME = 'legacy-volume'
SKIP = {'hf-cache', 'lost+found'}
MAX_BYTES = 200 * 1024 * 1024


class ArchiveError(Exception):
    pass


def build(root):
    root = Path(root)
    buf, files = io.BytesIO(), 0
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for path in sorted(root.rglob('*')):
            rel = path.relative_to(root)
            if rel.parts[0] in SKIP or not path.is_file() or path.is_symlink():
                continue
            tar.add(path, arcname=str(rel), recursive=False)
            files += 1
            if buf.tell() > MAX_BYTES:
                raise ArchiveError('Legacy data is larger than the archive limit')
    return buf.getvalue(), files


def archive(root):
    blob, files = build(root)
    digest = hashlib.sha256(blob).hexdigest()
    with database.connect() as c:
        stored = c.execute('SELECT sha256 FROM legacy_archives WHERE name=%s', (NAME,)).fetchone()
    if stored:
        # Compare file contents: the archive bytes themselves change with every build (gzip and tar timestamps).
        if any(diff(root).values()):
            raise ArchiveError('A different legacy archive already exists; refusing to overwrite it')
        return {'status': 'already-archived', 'files': files, 'size': len(blob), 'sha256': stored['sha256'][:16]}
    with database.connect() as c:
        c.execute('INSERT INTO legacy_archives(name,created,files,size,sha256,data) VALUES(%s,%s,%s,%s,%s,%s)',
                  (NAME, time.time(), files, len(blob), digest, gdrive._fernet().encrypt(blob)))
    if hashlib.sha256(_load()).hexdigest() != digest:
        raise ArchiveError('The stored archive did not read back intact')
    return {'status': 'archived', 'files': files, 'size': len(blob), 'sha256': digest[:16]}


def _load():
    with database.connect() as c:
        row = c.execute('SELECT data FROM legacy_archives WHERE name=%s', (NAME,)).fetchone()
    if not row:
        raise ArchiveError('No legacy archive stored')
    return gdrive._fernet().decrypt(bytes(row['data']))


def _files(root):
    root = Path(root)
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if rel.parts[0] not in SKIP and path.is_file() and not path.is_symlink():
            yield str(rel), path


def diff(root):
    """Which files differ from the stored archive. Names only, never contents."""
    with tarfile.open(fileobj=io.BytesIO(_load()), mode='r:gz') as tar:
        stored = {m.name: hashlib.sha256(tar.extractfile(m).read()).hexdigest() for m in tar.getmembers() if m.isfile()}
    current = {rel: hashlib.sha256(path.read_bytes()).hexdigest() for rel, path in _files(root)}
    return {'added': sorted(set(current) - set(stored)), 'removed': sorted(set(stored) - set(current)),
            'changed': sorted(k for k in current if k in stored and current[k] != stored[k])}


def restore(dest):
    dest = Path(dest).resolve()
    with tarfile.open(fileobj=io.BytesIO(_load()), mode='r:gz') as tar:
        members = tar.getmembers()
        for m in members:
            target = (dest / m.name).resolve()
            if not m.isfile() or dest not in target.parents:
                raise ArchiveError('Archive contains an unsafe entry')
        for m in members:
            target = dest / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())
    return {'status': 'restored', 'files': len(members)}


def purge(say=print):
    """Delete the archive for good, after reporting what it held (counts and dates only)."""
    with database.connect() as c:
        row = c.execute('SELECT files,size,created FROM legacy_archives WHERE name=%s FOR UPDATE', (NAME,)).fetchone()
        if not row:
            raise ArchiveError('No legacy archive stored')
        say(json.dumps({'files': row['files'], 'size': row['size'],
                        'created': time.strftime('%Y-%m-%d', time.gmtime(row['created']))}))
        c.execute('DELETE FROM legacy_archives WHERE name=%s', (NAME,))
    return {'status': 'purged'}


if __name__ == '__main__':
    database.wait_for_schema(0)  # an operator console never migrates: only the web process does
    try:
        if len(sys.argv) == 3 and sys.argv[1] == '--restore':
            print(json.dumps(restore(sys.argv[2])))
        elif len(sys.argv) == 3 and sys.argv[1] == '--diff':
            print(json.dumps(diff(sys.argv[2])))
        elif sys.argv[1:] == ['--purge']:
            print(json.dumps(purge()))
        elif len(sys.argv) == 2 and not sys.argv[1].startswith('-'):
            print(json.dumps(archive(sys.argv[1])))
        else:
            sys.exit('usage: python -m app.archive_legacy <legacy-dir> | --restore <dir> | --diff <dir> | --purge')
    except ArchiveError as e:
        sys.exit(str(e))
