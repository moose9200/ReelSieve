"""Operator console for accounts (there is no public admin setup).

    python -m app.admin create-admin <email>     # password read from stdin
    python -m app.admin set-password <email>     # password read from stdin; signs the account out everywhere
    python -m app.admin list                     # emails and roles only
    python -m app.admin set-plan <email> <plan> <credits>   # e.g. give free credits
"""
import sys

from app import auth, database, plans, store


def main(argv):
    if argv[:1] == ['list'] and len(argv) == 1:
        for u in auth.users():
            print(u['user'], u['role'])
        return 0
    if argv[:1] == ['set-plan'] and len(argv) == 4:
        email, plan, credits = argv[1].strip().lower(), argv[2], argv[3]
        if plan not in plans.PLANS or not credits.isdigit() or int(credits) > 100000:
            print(f'Plan must be one of {", ".join(plans.PLANS)}; credits 0-100000', file=sys.stderr)
            return 2
        try:
            store.set_plan(email, plan, int(credits), note='set by operator console')
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(email, plan, int(credits))
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
