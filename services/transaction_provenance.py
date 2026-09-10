"""Server-side transaction provenance policy.

The browser/mobile client may describe a transaction, but it never gets to
promote its own data to lender-grade VERIFIED evidence.  Only trusted server
workflows may assign VERIFIED.
"""

VERIFIED = "VERIFIED"
SELF_REPORTED = "SELF_REPORTED"
SELF_CONFIRMED = "SELF_CONFIRMED"
UNVERIFIED = "UNVERIFIED"
FAILED = "FAILED"
PENDING = "PENDING"

TRUSTED_VERIFICATION_SOURCES = {
    "BANK_API",
    "ACCOUNT_AGGREGATOR",
    "PAYMENT_GATEWAY_WEBHOOK",
    "INSTITUTION_REVIEW",
}


def consumer_manual_provenance():
    return SELF_REPORTED, "MANUAL_ENTRY", None, None


def consumer_upi_confirmation_provenance(reference=None):
    # A success/click returned by a consumer-controlled UPI flow is useful to
    # the personal-finance UX, but is not independent bank confirmation.
    return SELF_CONFIRMED, "UPI_USER_CONFIRMATION", None, reference


def document_import_provenance(reference=None):
    return UNVERIFIED, "DOCUMENT_IMPORT", None, reference


def csv_import_provenance(reference=None):
    return UNVERIFIED, "CSV_IMPORT", None, reference
