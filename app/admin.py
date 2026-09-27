"""Operator console for accounts (there is no public admin setup).

    python -m app.admin create-admin <email>     # password read from stdin
    python -m app.admin set-password <email>     # password read from stdin; signs the account out everywhere
    python -m app.admin list                     # emails and roles only
    python -m app.admin set-plan <email> <plan> <credits>   # e.g. give free credits
    python -m app.admin deactivate <email>       # stops their jobs, revokes their Drive grant, keeps history
    python -m app.admin erase <email>            # erases their personal data; paid orders kept for tax records
    python -m app.admin unsuppress <email>       # undoes the "Do not contact" marks that account made (misuse only)
"""
import secrets
import sys
import time

from app import auth, database, gdrive, jobs, plans, store


PHOTOS_LEFT = ('Some uploaded photos could not be deleted from Google Drive. They are in the Inputs folder inside '
               'the ReelSieve folder there.')


def _drop_photo_inputs(owner):
    """Photos the customer asked us to delete from their Drive go while the Drive grant still works: once it is revoked
    (and, on erasure, the job rows are gone) the clean-up sweeper can no longer reach them. Returns a warning or None."""
    with database.connect() as c:
        rows = c.execute("SELECT id,params FROM jobs WHERE owner_id=%s AND params->>'source'='photos' "
                         "AND params->>'delete_inputs'='true' AND COALESCE(meta->>'inputs','')<>'deleted'", (owner,)).fetchall()
    left = False
    for job in rows:
        p = job['params'].get('photos') or {}
        try:
            # generation None: whichever connection is current; with drive.file only the app's own files are reachable
            gdrive.delete_inputs(None, p['folder'], None, p.get('ids') or (), owner_id=owner)
        except Exception:  # best effort: the account change must go ahead; the caller reports where the photos are
            left = True
            continue
        with database.connect() as c:
            c.execute("UPDATE jobs SET meta=meta || '{\"inputs\": \"deleted\"}'::jsonb WHERE id=%s", (job['id'],))
    return PHOTOS_LEFT if left else None


def _warnings(*items):
    return ' '.join(w for w in items if w) or None


def deactivate(email, by=None):
    """Deactivate an account, stop its jobs and revoke its Drive grant. Returns a warning or None."""
    auth.delete_user(email, by)
    store.admin_event('deactivate', by, email)
    owner = database.user_id(email)
    jobs.cancel_owner(owner)
    left = _drop_photo_inputs(owner)
    try:
        gdrive.disconnect_owner(owner)
    except RuntimeError as e:
        return _warnings(str(e), left)
    return left


def erase(email, by=None, via=None):
    """Erase an account (UK/EU GDPR Art 17, DPDP s12), also one already deactivated. Returns a warning or None.

    Sign-in ends, jobs stop, the Drive grant is revoked, then one transaction deletes jobs, outreach, Drive
    receipts, OAuth states, sign-in network hashes and unpaid orders; deletes their unrewarded referrals, removes
    their side of rewarded ones and retires their invite code;
    strips accounts, usage and paid orders to what accounting and the statutory record period need; and anonymises
    the users row (the email becomes free for a new signup).
    by: the admin, the account itself (self-service) or None (operator console / retention)."""
    via = via or ('console' if by is None else 'self' if auth.norm(by) == auth.norm(email) else 'admin')
    owner = auth.begin_erase(email, by)
    jobs.cancel_owner(owner)
    warning = _drop_photo_inputs(owner)
    try:
        gdrive.disconnect_owner(owner)
    except RuntimeError as e:
        warning = _warnings(str(e), warning)
    now = time.time()
    with database.connect() as c:
        # Row lock first: concurrent erasures (retention on several workers) queue here instead of deadlocking.
        row = c.execute('SELECT email FROM users WHERE id=%s AND erased_at IS NULL FOR UPDATE', (owner,)).fetchone()
        if not row:
            return warning  # another worker finished erasing it first
        address = row['email']
        store.admin_event('erase', by, address, conn=c, via=via)
        for table in ('jobs', 'outreach', 'drive_uploads', 'drive_oauth_states', 'signin_networks'):
            c.execute(f'DELETE FROM {table} WHERE owner_id=%s', (owner,))
        # Referrals: a row with no reward holds nothing the other account needs, so it goes. A rewarded row keeps only
        # the other account's side (its own bonus). Nothing new is written about the erased account.
        c.execute('DELETE FROM referrals WHERE (referrer_id=%s OR referee_id=%s) AND rewarded_at IS NULL', (owner, owner))
        c.execute('UPDATE referrals SET referrer_id=NULL WHERE referrer_id=%s', (owner,))
        c.execute('UPDATE referrals SET referee_id=NULL,google_hash=NULL WHERE referee_id=%s', (owner,))
        c.execute('DELETE FROM referrals WHERE referrer_id IS NULL AND referee_id IS NULL')
        c.execute("UPDATE drive_connections SET status='disconnected',credentials=NULL,google_sub=NULL,google_email=NULL,"
                  'scope=NULL,folder_id=NULL,connected_at=NULL,updated=%s WHERE owner_id=%s', (now, owner))
        c.execute('UPDATE usage SET listing_key=NULL,fp_hash=NULL WHERE owner_id=%s', (owner,))  # ip_hash: 90-day abuse window
        c.execute('UPDATE accounts SET ip_hash=NULL,fp_hash=NULL,note=NULL,referral_code=NULL,b2b_sender=NULL WHERE owner_id=%s', (owner,))
        # the objections they recorded stay honoured; the account link becomes a keyed hash of the email (never the
        # email), so unsuppress <email> can still undo misuse until retention clears it 90 days after the mark
        c.execute('UPDATE outreach_suppressions SET owner_id=NULL,owner_hash=%s WHERE owner_id=%s',
                  (store.suppression_owner_hash(address), owner))
        c.execute("DELETE FROM orders WHERE owner_id=%s AND status IN ('pending','cancelled')", (owner,))
        # Paid, or reported paid and awaiting confirmation: the tax record needs the payer's email if the money clears.
        c.execute("UPDATE orders SET note=NULL,meta=NULL,pay_link=NULL,billing_email=COALESCE(billing_email,%s) WHERE owner_id=%s",
                  (address, owner))
        c.execute("UPDATE users SET email=%s,salt=%s,hash=%s,role='member',active=FALSE,erased_at=%s,"
                  'session_version=session_version+1 WHERE id=%s',
                  (f'deleted-{owner}@erased.invalid', secrets.token_hex(16), 'erased:' + secrets.token_hex(32), now, owner))
    return warning


def unsuppress(email):
    """Undo the "Do not contact" marks one account made, e.g. someone hiding companies from every other customer, also
    after that account was erased (by the keyed hash erasure leaves). Objections through the privacy form have no
    account and are never touched here (Settings undoes those per request); marks older than 90 days have lost their
    account link (app/retention.py) and stay. The event names an erased account by nothing at all."""
    email = auth.norm(email)
    with database.connect() as c:
        row = c.execute('SELECT id FROM users WHERE email=%s', (email,)).fetchone()
        n = c.execute('DELETE FROM outreach_suppressions WHERE owner_id=%s OR owner_hash=%s',
                      (row['id'] if row else None, store.suppression_owner_hash(email))).rowcount
        store.admin_event('unsuppress', None, email if row else None, conn=c, rows=n)
    return n


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
            store.admin_event('plan', None, email, plan=plan, credits=int(credits))
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(email, plan, int(credits))
        return 0
    if argv[:1] == ['deactivate'] and len(argv) == 2:
        try:
            warning = deactivate(argv[1])
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print('deactivated', auth.norm(argv[1]) + (f' (warning: {warning})' if warning else ''))
        return 0
    if argv[:1] == ['erase'] and len(argv) == 2:
        try:
            warning = erase(argv[1])
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print('erased', auth.norm(argv[1]) + (f' (warning: {warning})' if warning else ''))
        return 0
    if argv[:1] == ['unsuppress'] and len(argv) == 2:
        try:
            n = unsuppress(argv[1])
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print('removed', n, 'do-not-contact marks made by', auth.norm(argv[1]))
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
            store.admin_event('password_reset', None, argv[1].strip().lower())
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print('ok')
    return 0


if __name__ == '__main__':
    database.initialize()
    sys.exit(main(sys.argv[1:]))
