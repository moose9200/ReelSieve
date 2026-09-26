"""Operator console for accounts (there is no public admin setup).

    python -m app.admin create-admin <email>     # password read from stdin
    python -m app.admin set-password <email>     # password read from stdin; signs the account out everywhere
    python -m app.admin list                     # emails and roles only
"""
import sys

from app import auth, database


def main(argv):
    if argv[:1] == ['list'] and len(argv) == 1:
        for u in auth.users():
            print(u['user'], u['role'])
        return 0
    if len(argv) != 2 or argv[0] not in ('create-admin', 'set-password'):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    password = sys.stdin.readline().rstrip('\n')
    try:
        if argv[0] == 'create-admin':
            auth.create_user(argv[1], password, 'admin')
        else:
            auth.set_password(argv[1], password)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print('ok')
    return 0


if __name__ == '__main__':
    database.initialize()
    sys.exit(main(sys.argv[1:]))
