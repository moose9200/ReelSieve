"""Retention schedule, enforced in code; the worker runs it hourly next to store.purge_signals.
The privacy notice states these periods: change both together.

India, for invoices and accounts (legal research 26 Sep 2026, compliance/2026-09-26_legal-requirements.md):
CGST Act s.36 keeps records 72 months from the annual-return due date; Income-tax Rules 2026 r.46 keeps books
seven tax years from the end of the relevant tax year. Counted from the payment date, both end within 8 years.
Records under appeal or investigation must be kept longer: hold them outside this schedule.
"""
import json
import os
import time

from app import admin, database

DAY = 86400
YEAR = 365.25 * DAY
FINANCIAL_RECORDS_YEARS = 8
LOGIN_FAILURE_HOURS = 24
THIRD_PARTY_JOB_DAYS = 30      # host name, host message, review data in a finished job
OUTREACH_MONTHS = 12           # after the row's last change
DEACTIVATED_DAYS = 30          # "Remove" deactivates; erasure follows
ADMIN_EVENT_YEARS = 2
PRIVACY_REQUEST_YEARS = 2      # after the request was handled
ERASED_UNPAID_ORDER_DAYS = 90  # a payment reported before erasure that never cleared
INVOICE_BACKUP_RUN_YEARS = 2   # records of the daily India backup (app/invoices.py); no personal data
REFERRAL_REWARDED_YEARS = 2    # after the reward
REFERRAL_UNREWARDED_YEARS = 1  # after signup, when never rewarded
SUPPRESSION_OWNER_DAYS = 90    # which account marked "Do not contact"; the suppression itself is kept for good

# A job keeps the customer's own reel history; only other people's data goes. Legacy jobs stored the finished
# host message as the customer's template too. Guest quotes typed for an own-photo reel are review data as well.
STRIP_JOBS = ("UPDATE jobs SET meta=(meta #- '{listing,host}') - 'message' - 'review_used',"
              "params=(CASE WHEN meta->>'legacy'='true' THEN params - 'message' ELSE params END) - 'quotes' "
              "WHERE finished_at<%s AND (meta #> '{listing,host}' IS NOT NULL OR meta ? 'message' OR meta ? 'review_used' "
              "OR (meta->>'legacy'='true' AND params ? 'message') OR params ? 'quotes')")


def run(now=None):
    """Delete or strip everything past its period. Idempotent and safe on several workers at once. Returns counts."""
    now = now or time.time()
    keep_archive = float(os.getenv('LEGACY_ARCHIVE_KEEP_DAYS', '90'))
    with database.connect() as c:
        out = {name: c.execute(q, (cutoff,)).rowcount for name, q, cutoff in [
            ('login_failures', 'DELETE FROM login_failures WHERE ts<%s', now - LOGIN_FAILURE_HOURS * 3600),
            ('drive_oauth_states', 'DELETE FROM drive_oauth_states WHERE expires_at<%s', now),
            ('job_third_party', STRIP_JOBS, now - THIRD_PARTY_JOB_DAYS * DAY),
            ('outreach', 'DELETE FROM outreach WHERE GREATEST(ts,sent_at,updated)<%s', now - OUTREACH_MONTHS * YEAR / 12),
            ('paid_orders', "DELETE FROM orders WHERE status='paid' AND paid_at<%s", now - FINANCIAL_RECORDS_YEARS * YEAR),
            ('erased_unpaid_orders', "DELETE FROM orders o USING users u WHERE u.id=o.owner_id AND o.status<>'paid' "
                                     'AND u.erased_at<%s', now - ERASED_UNPAID_ORDER_DAYS * DAY),
            ('admin_events', 'DELETE FROM admin_events WHERE ts<%s', now - ADMIN_EVENT_YEARS * YEAR),
            ('privacy_requests', "DELETE FROM privacy_requests WHERE status='handled' AND handled_at<%s",
             now - PRIVACY_REQUEST_YEARS * YEAR),
            ('referrals_rewarded', 'DELETE FROM referrals WHERE rewarded_at<%s', now - REFERRAL_REWARDED_YEARS * YEAR),
            ('referrals_unrewarded', 'DELETE FROM referrals WHERE rewarded_at IS NULL AND ts<%s',
             now - REFERRAL_UNREWARDED_YEARS * YEAR),
            ('legacy_archives', 'DELETE FROM legacy_archives WHERE created<%s', now - keep_archive * DAY),
            ('invoice_backups', 'DELETE FROM invoice_backups WHERE ts<%s', now - INVOICE_BACKUP_RUN_YEARS * YEAR),
            ('suppression_owner', 'UPDATE outreach_suppressions SET owner_id=NULL WHERE owner_id IS NOT NULL AND ts<%s',
             now - SUPPRESSION_OWNER_DAYS * DAY),
        ]}
        due = [r['email'] for r in c.execute('SELECT email FROM users WHERE NOT active AND erased_at IS NULL AND deactivated_at<%s',
                                             (now - DEACTIVATED_DAYS * DAY,)).fetchall()]
    out['erased'] = 0
    for email in due:
        try:
            admin.erase(email, via='retention')
            out['erased'] += 1
        except ValueError:
            pass  # another worker erased it first
    if any(out.values()):
        print(json.dumps({'retention': out}), flush=True)  # counts only
    return out
