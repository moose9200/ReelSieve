"""Retention schedule, enforced in code. The privacy notice states these periods; change both together.

India, for invoices and accounts (legal research 26 Sep 2026, compliance/2026-09-26_legal-requirements.md):
CGST Act s.36 keeps records 72 months from the annual-return due date; Income-tax Rules 2026 r.46 keeps books
seven tax years from the end of the relevant tax year. Counted from the payment date, both end within 8 years.
Records under appeal or investigation must be kept longer: hold them outside this schedule.
"""
FINANCIAL_RECORDS_YEARS = 8
