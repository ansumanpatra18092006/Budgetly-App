from flask import Blueprint, request, jsonify, session, make_response
from utils.decorators import login_required
from utils.db import get_db
from datetime import datetime
from difflib import SequenceMatcher
from psycopg import sql
import csv
import io
import re
import time
import logging

from ml.category_model import predict_category

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Debug Flags
# ---------------------------------------------------------------------------

DEBUG_PARSER = False  # Set True locally for verbose parsing logs

# Set True locally to log a per-phase timing breakdown of
# /import-preview-transactions (request → validate → insert → commit →
# response). Leave False in production; a single concise summary line
# is always logged regardless (see _log_import_summary below).
IMPORT_PROFILE = True

def _pdebug(msg: str, *args) -> None:
    if DEBUG_PARSER:
        logger.debug(msg, *args)

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

transactions_bp = Blueprint("transactions", __name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_CATEGORIES = {
    "Food",
    "Transport",
    "Shopping",
    "Health",
    "Education",
    "Entertainment",
    "Housing",
    "Finance",
    "Travel",
    "Misc"
}
MERCHANT_MAP = {
    # Entertainment
    "netflix":      "Entertainment",
    "spotify":      "Entertainment",
    "youtube":      "Entertainment",
    "hotstar":      "Entertainment",
    "disney":       "Entertainment",
    "prime video":  "Entertainment",
    "jiocinema":    "Entertainment",
    "zee5":         "Entertainment",
    "sonyliv":      "Entertainment",
    # Shopping
    "flipkart":     "Shopping",
    "amazon":       "Shopping",
    "meesho":       "Shopping",
    "myntra":       "Shopping",
    "nykaa":        "Shopping",
    "ajio":         "Shopping",
    "snapdeal":     "Shopping",
    "blinkit":      "Shopping",
    "zepto":        "Shopping",
    "bigbasket":    "Shopping",
    "dmart":        "Shopping",
    "reliance":     "Shopping",
    # Food
    "zomato":       "Food",
    "swiggy":       "Food",
    "dominos":      "Food",
    "mcdonalds":    "Food",
    "kfc":          "Food",
    "pizza hut":    "Food",
    "burger king":  "Food",
    "starbucks":    "Food",
    "amul":         "Food",
    # Housing / Utilities
    "jio":          "Housing",
    "airtel":       "Housing",
    "bsnl":         "Housing",
    "electricity":  "Housing",
    "bescom":       "Housing",
    "msedcl":       "Housing",
    "tata power":   "Housing",
    "gas":          "Housing",
    "water":        "Housing",
    "broadband":    "Housing",
    "wifi":         "Housing",
    # Transport
    "irctc":        "Transport",
    "railway":      "Transport",
    "uber":         "Transport",
    "ola":          "Transport",
    "rapido":       "Transport",
    "redbus":       "Transport",
    "makemytrip":   "Travel",
    "goibibo":      "Travel",
    "cleartrip":    "Travel",
    "indigo":       "Travel",
    "air india":    "Travel",
    "spicejet":     "Travel",
    # Health
    "hospital":     "Health",
    "medical":      "Health",
    "pharmacy":     "Health",
    "apollo":       "Health",
    "medplus":      "Health",
    "1mg":          "Health",
    "practo":       "Health",
    "netmeds":      "Health",
    # Finance
    "insurance":    "Finance",
    "loan":         "Finance",
    "lic":          "Finance",
    "zerodha":      "Finance",
    "groww":        "Finance",
    "upstox":       "Finance",
    "paytm money":  "Finance",
    "sip":          "Finance",
    "mutual fund":  "Finance",
    # Education
    "udemy":        "Education",
    "coursera":     "Education",
    "byju":         "Education",
    "unacademy":    "Education",
    "vedantu":      "Education",
    "duolingo":     "Education",
}

CATEGORY_KEYWORDS = {
    "Food":          ["hotel", "restaurant", "cafe", "biryani", "dhaba", "food",
                      "kitchen", "bakery", "canteen", "tiffin", "meals", "lunch",
                      "dinner", "breakfast", "snack", "juice", "tea", "coffee"],
    "Transport":     ["petrol", "fuel", "diesel", "auto", "taxi", "bus", "metro",
                      "cab", "rickshaw", "parking", "toll", "fastag", "train"],
    "Housing":       ["rent", "electricity", "bill", "wifi", "broadband", "water",
                      "maintenance", "society", "flat", "pg", "hostel", "gas",
                      "cylinder", "lpg"],
    "Health":        ["clinic", "pharmacy", "doctor", "hospital", "medical",
                      "medicine", "health", "diagnostic", "lab", "nursing",
                      "dental", "eye", "pathology", "test", "scan"],
    "Shopping":      ["store", "shop", "mall", "purchase", "market", "bazaar",
                      "supermarket", "hypermarket", "retail", "mart"],
    "Finance":       ["loan", "emi", "interest", "insurance", "tax", "mutual",
                      "fund", "sip", "invest", "policy", "premium", "nps",
                      "ppf", "fd", "rd", "dividend", "trading"],
    "Entertainment": ["netflix", "hotstar", "prime", "spotify", "youtube",
                      "cinema", "movie", "game", "play", "entertainment",
                      "theatre", "concert", "event", "ticket", "streaming"],
    "Education":     ["school", "college", "university", "course", "tuition",
                      "coaching", "books", "study", "exam", "fee", "library",
                      "stationery"],
    "Travel":        ["flight", "train", "irctc", "booking", "hotel", "resort",
                      "airbnb", "lodge", "travel", "tour", "trip", "holiday",
                      "vacation", "airline", "airport"],
}

GARBAGE_MARKERS = [
    "transactionstatementperiod",
    "note:thisstatement",
    "googlepayapp",
    "openingbalance",
    "closingbalance",
    "totalcredit",
    "totaldebit",
    "statementofaccount",
    "customercare",
    "grievance",
    "terms and conditions",
    "disclaimer",
    "generated on",
    "this is a system",
    "authorized signatory",
    "phonepetransaction",
    "phonepe statement",
    "download",
    "page no",
]

BANK_ARTIFACT_PATTERNS = [
    r"bank\s*of\s*baroda\s*\d*",
    r"\bsbi\b\s*\d*",
    r"state\s*bank\s*of\s*india\s*\d*",
    r"hdfc\s*bank\s*\d*",
    r"icici\s*bank\s*\d*",
    r"axis\s*bank\s*\d*",
    r"kotak\s*bank\s*\d*",
    r"punjab\s*national\s*bank\s*\d*",
    r"canara\s*bank\s*\d*",
    r"union\s*bank\s*\d*",
    r"bank\s*of\s*india\s*\d*",
    r"yes\s*bank\s*\d*",
    r"indusind\s*bank\s*\d*",
    r"federal\s*bank\s*\d*",
    r"idfc\s*first\s*bank\s*\d*",
    r"\b\d{9,}\b",           # long numeric strings (account/ref numbers)
    r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}",  # UPI IDs (email-like)
    r"utr\s*:?\s*\d+",
    r"ref\s*no\.?\s*:?\s*\d+",
    r"txn\s*id\s*:?\s*[\w\d]+",
]

# ---------------------------------------------------------------------------
# Compiled Regex Patterns
# ---------------------------------------------------------------------------

_WS_RE          = re.compile(r"\s+")
_TIME_RE        = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)\b", re.IGNORECASE)
_CAMEL_RE       = re.compile(r"([a-z])([A-Z])")
_AMOUNT_RE      = re.compile(r"₹\s?([\d,]+\.?\d*)")
_AMOUNT_SIGN_RE = re.compile(r"₹\s?([\d,]+\.?\d*)\s*(?:CR|DR)?", re.IGNORECASE)

# Google Pay patterns
_GPY_TXID_RE    = re.compile(r"(?=UPITransactionID:\d+)")
_GPY_PAID_RE    = re.compile(r"Paidto(.+?)(?=₹|UPI|$)", re.DOTALL)
_GPY_RECV_RE    = re.compile(r"Receivedfrom(.+?)(?=₹|UPI|$)", re.DOTALL)

# PhonePe patterns (spaced text)
_PPE_TXID_RE = re.compile(r"Transaction\s+ID\s*[:\-]?\s*[A-Z0-9]+", re.IGNORECASE)
_PPE_PAID_RE = re.compile(
    r"(?:Paid\s+to|Sent\s+to|Transferred\s+to)\s+(.+?)\s+(?:DEBIT|₹)",
    re.IGNORECASE | re.DOTALL
)
_PPE_RECV_RE    = re.compile(
    r"(?:Received\s+from|Credited\s+from|Refund\s+from)\s+(.+?)(?=₹|\bCREDIT\b|\bTransaction\b|$)",
    re.IGNORECASE | re.DOTALL
)
_PPE_DEBIT_RE   = re.compile(r"\bDEBIT\b", re.IGNORECASE)
_PPE_CREDIT_RE  = re.compile(r"\bCREDIT\b", re.IGNORECASE)

# Date patterns
_DATE_GPY_RE    = re.compile(r"(\d{2}[A-Za-z]{3},\s*\d{4})")
_DATE_PPE_RE    = re.compile(
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s,]+\d{4})",
    re.IGNORECASE
)
_DATE_PPE_ALT_RE = re.compile(
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE
)
_DATE_ISO_RE    = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATE_DMY_RE    = re.compile(r"(\d{2}[\/\-]\d{2}[\/\-]\d{4})")

# Self-transfer markers
_SELF_TRANSFER_KEYWORDS = {
    "self", "own account", "myself", "my account",
    "savings account", "current account", "wallet transfer"
}


# ---------------------------------------------------------------------------
# Format Detection
# ---------------------------------------------------------------------------

class StatementFormat:
    GPAY    = "gpay"
    PHONEPE = "phonepe"
    UNKNOWN = "unknown"


def _detect_format(text: str) -> str:
    """
    Auto-detect the UPI statement format from the raw PDF text.
    Returns StatementFormat constant.
    """
    norm = text[:2000].lower()  # inspect header only for speed

    if "upitransactionid" in norm or "paidto" in norm or "receivedfrom" in norm:
        _pdebug("Format detected: Google Pay")
        return StatementFormat.GPAY

    if (
        "phonepe" in norm
        or "transaction id" in norm
        or ("paid to" in norm and ("debit" in norm or "credit" in norm))
    ):
        _pdebug("Format detected: PhonePe")
        return StatementFormat.PHONEPE

    # Fallback: heuristic on amount + type keywords
    if re.search(r"(?:paid|received)\s+(?:to|from)", text[:3000], re.IGNORECASE):
        _pdebug("Format detected: PhonePe (heuristic)")
        return StatementFormat.PHONEPE

    _pdebug("Format detection inconclusive — defaulting to GPay")
    return StatementFormat.GPAY


# ---------------------------------------------------------------------------
# Text Utilities
# ---------------------------------------------------------------------------

def normalize_text(s: str) -> str:
    """Lowercase, alphanumeric-only, single-spaced string."""
    if not s:
        return ""
    s = re.sub(r"[^a-zA-Z0-9 ]", " ", s)
    return _WS_RE.sub(" ", s).lower().strip()


def _split_camel_case(text: str) -> str:
    """Insert space at camelCase boundaries: NetflixEnt → Netflix Ent"""
    return _CAMEL_RE.sub(r"\1 \2", text)


def _remove_bank_artifacts(text: str) -> str:
    """Strip bank names, account numbers, UPI IDs, UTR refs from text."""
    for pattern in BANK_ARTIFACT_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return text


def _clean_description(raw: str) -> str:
    """
    Convert raw compressed/spaced PDF description text into a clean,
    human-readable merchant name.

    Pipeline (order is critical):
      1. Split camelCase before ANY lowercasing
      2. Remove bank artifacts
      3. Strip embedded dates
      4. Strip UPI tokens
      5. Strip leftover trigger words
      6. Keep only merchant-safe characters
      7. Collapse whitespace + title-case
    """
    # 1. Split camelCase (must happen before lowercasing)
    text = _split_camel_case(raw)
    # 2. Remove bank names, account numbers, UPI IDs
    text = _remove_bank_artifacts(text)
    # 3. Strip embedded dates (GPay format: 02Sep,2025  PhonePe: 02 Sep 2025)
    text = re.sub(r"\d{1,2}\s*[A-Za-z]{3,9}\s*,?\s*\d{4}", "", text)
    # 4. Strip UPI metadata tokens
    text = re.sub(r"\bUPI\S*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTransaction\s*ID\b.*", "", text, flags=re.IGNORECASE)
    # 5. Strip leftover trigger words
    text = re.sub(
        r"\b(Paidto|Paid\s+to|Receivedfrom|Received\s+from|Sent\s+to|"
        r"Transferred\s+to|Credited\s+from|Refund\s+from|DEBIT|CREDIT)\b",
        "", text, flags=re.IGNORECASE
    )
    # 6. Keep only printable merchant-safe characters
    text = re.sub(r"[^a-zA-Z0-9&'\-\. ]", " ", text)
    # 7. Collapse whitespace + title-case
    text = _WS_RE.sub(" ", text).strip()
    return text.title() if text else "Unknown"


def _is_garbage_block(block: str) -> bool:
    """Return True if block is a header, footer, or non-transaction artifact."""
    b = normalize_text(block)
    return any(marker in b for marker in GARBAGE_MARKERS)


def _is_self_transfer(description: str) -> bool:
    """Detect probable self-transfers (wallet top-ups, own-account moves)."""
    d = description.lower()
    return any(kw in d for kw in _SELF_TRANSFER_KEYWORDS)


# ---------------------------------------------------------------------------
# Amount Extraction
# ---------------------------------------------------------------------------

def _extract_amount(block: str) -> float | None:
    """
    Return the correct transaction amount from a block.

    Strategy:
    - Collect all ₹ values in the block.
    - The LAST one is almost always the line-item amount;
      earlier values are running totals / header figures.
    - Additionally skip values that are implausibly large
      relative to the others (> 10× median) to catch header totals.
    """
    matches = _AMOUNT_RE.findall(block)
    if not matches:
        return None

    amounts: list[float] = []
    for m in matches:
        try:
            amounts.append(float(m.replace(",", "")))
        except ValueError:
            continue

    if not amounts:
        return None

    if len(amounts) == 1:
        return amounts[0] if amounts[0] > 0 else None

    # Filter out implausibly large outliers (header totals)
    sorted_a = sorted(amounts)
    median   = sorted_a[len(sorted_a) // 2]
    filtered = [a for a in amounts if a <= max(median * 20, 1_00_000)]
    if not filtered:
        filtered = amounts

    return filtered[-1] if filtered[-1] > 0 else None


# ---------------------------------------------------------------------------
# Date Parsing
# ---------------------------------------------------------------------------

def _parse_date(block: str) -> str | None:
    """
    Try multiple date formats found in GPay and PhonePe statements.
    Returns ISO date string "YYYY-MM-DD" or None.
    """
    # 1. GPay compressed:  02Sep,2025  /  02 Sep, 2025
    m = _DATE_GPY_RE.search(block)
    if m:
        raw = m.group(1).replace(" ", "")
        try:
            fixed = re.sub(r"(\d{2})([A-Za-z]{3}),(\d{4})", r"\1 \2 \3", raw)
            return datetime.strptime(fixed, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 2. PhonePe spaced:  2 Sep 2025  /  02 September, 2025
    # 0. PhonePe format: Apr 09, 2026
    m = _DATE_PPE_ALT_RE.search(block)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            return datetime.strptime(raw, "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 3. ISO:  2025-09-02
    m = _DATE_ISO_RE.search(block)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 4. DD/MM/YYYY or DD-MM-YYYY
    m = _DATE_DMY_RE.search(block)
    if m:
        raw = m.group(1).replace("-", "/")
        try:
            return datetime.strptime(raw, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Google Pay Parser
# ---------------------------------------------------------------------------

def _parse_gpay_blocks(text: str) -> list[dict]:
    """
    Parse compressed Google Pay statement text.
    Splits on 'UPITransactionID:' boundaries.
    """
    raw_blocks = _GPY_TXID_RE.split(text)
    results: list[dict] = []

    for idx, block in enumerate(raw_blocks):
        block = block.strip()
        if not block:
            continue

        _pdebug("[GPay block %d] %.120s", idx, block)

        if _is_garbage_block(block):
            _pdebug("[GPay block %d] SKIPPED — garbage", idx)
            continue

        # Type detection
        if "Receivedfrom" in block:
            t_type     = "income"
            desc_match = _GPY_RECV_RE.search(block)
        elif "Paidto" in block:
            t_type     = "expense"
            desc_match = _GPY_PAID_RE.search(block)
        else:
            _pdebug("[GPay block %d] SKIPPED — no type marker", idx)
            continue

        if not desc_match:
            _pdebug("[GPay block %d] SKIPPED — desc pattern miss", idx)
            continue

        date = _parse_date(block)
        if not date:
            _pdebug("[GPay block %d] SKIPPED — no date", idx)
            continue

        description = _clean_description(desc_match.group(1))
        if not description or description == "Unknown":
            _pdebug("[GPay block %d] SKIPPED — empty description", idx)
            continue

        amount = _extract_amount(block)
        if amount is None or amount <= 0:
            _pdebug("[GPay block %d] SKIPPED — bad amount", idx)
            continue

        results.append({
            "description": description,
            "amount":      amount,
            "type":        t_type,
            "date":        date,
            "time":        _extract_time(block),
            "transaction_timestamp": _build_timestamp(date, _extract_time(block)),
            "reference_id": _extract_reference_id(block),
            "utr": _extract_utr(block),
        })

    return results


# ---------------------------------------------------------------------------
# PhonePe Parser
# ---------------------------------------------------------------------------

def _parse_phonepe_blocks(text: str) -> list[dict]:
    import re

    # 🔥 Split by DATE (this is stable in PhonePe)
    raw_blocks = re.split(
        r"(?=\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4})",
        text,
        flags=re.IGNORECASE
    )

    results = []

    for idx, block in enumerate(raw_blocks):
        block = block.strip()
        if not block:
            continue

        if _is_garbage_block(block):
            continue

        # ----------------------------
        # TYPE
        # ----------------------------
        
        block_lower = block.lower()

        paid_match = _PPE_PAID_RE.search(block)
        recv_match = _PPE_RECV_RE.search(block)

        if paid_match:
            t_type = "expense"
            desc_match = paid_match

        elif recv_match:
            t_type = "income"
            desc_match = recv_match

        else:
            continue

        if not desc_match:
            continue

        # ----------------------------
        # DATE
        # ----------------------------
        date = _parse_date(block)
        if not date:
            continue

        # ----------------------------
        # DESCRIPTION
        # ----------------------------
        description = _clean_description(desc_match.group(1))
        if not description or description == "Unknown":
            continue

        # ----------------------------
        # AMOUNT
        # ----------------------------
        amount = _extract_amount(block)
        if not amount:
            continue

        parsed_time = _extract_time(block)
        results.append({
            "description": description,
            "amount": amount,
            "type": t_type,
            "date": date,
            "time": parsed_time,
            "transaction_timestamp": _build_timestamp(date, parsed_time),
            "reference_id": _extract_reference_id(block),
            "utr": _extract_utr(block),
        })

    return results


# ---------------------------------------------------------------------------
# Universal Statement Parser
# ---------------------------------------------------------------------------

def parse_upi_statement(text: str, user_id=None) -> list[dict]:
    """
    Universal parser for Google Pay and PhonePe UPI PDF statements.

    Auto-detects the statement format and delegates to the appropriate
    format-specific parser.

    Returns transaction dicts with preserved source time/identity when available:
        {
            "description": str,
            "amount": float,
            "type": "income" | "expense",
            "category": str,
            "date": "YYYY-MM-DD",
            "time": "HH:MM:SS" | None,
            "transaction_timestamp": "YYYY-MM-DDTHH:MM:SS" | None,
            "reference_id": str | None,
            "utr": str | None,
            "source": str | None,
        }
    """
    if not text or not text.strip():
        logger.warning("parse_upi_statement: received empty text.")
        return []

    # Single whitespace normalisation pass
    text = _WS_RE.sub(" ", text)

    fmt = _detect_format(text)

    if fmt == StatementFormat.PHONEPE:
        raw_txns = _parse_phonepe_blocks(text)
    else:
        raw_txns = _parse_gpay_blocks(text)

    # 🔥 PERFORMANCE FIX: fetch user_category_map ONCE for this whole PDF,
    # instead of once per transaction inside get_smart_category(). The
    # in-memory map is then reused by every transaction below — merchant
    # map / keyword / ML priority order is unchanged.
    user_map = _fetch_user_category_map(user_id)

    transactions: list[dict] = []
    seen: set[tuple]         = set()

    for tx in raw_txns:
        # Deduplication key
        dedup_key = (
            tx.get("reference_id") or tx.get("utr") or
            (tx.get("date"), tx.get("transaction_timestamp"), round(tx["amount"], 2), tx["type"], normalize_text(tx["description"]))
        )
        if dedup_key in seen:
            _pdebug("Duplicate skipped: %s", dedup_key)
            continue
        seen.add(dedup_key)

        # Self-transfer tagging (logged but kept — category = Finance)
        if _is_self_transfer(tx["description"]):
            _pdebug("Self-transfer detected: %s", tx["description"])
            category = "Finance"
        else:
            try:
                category = get_smart_category(user_id, tx["description"], user_map=user_map)
            except Exception:
                logger.exception("get_smart_category failed for '%s'.", tx["description"])
                category = "Misc"

        transactions.append({
            "description": tx["description"],
            "amount":      tx["amount"],
            "type":        tx["type"],
            "category":    category,
            "date":        tx["date"],
            "time":        tx.get("time"),
            "transaction_timestamp": tx.get("transaction_timestamp"),
            "reference_id": tx.get("reference_id"),
            "utr": tx.get("utr"),
            "source": fmt.title(),
        })

    logger.info(
        "parse_upi_statement [%s]: %d transactions extracted.",
        fmt, len(transactions)
    )
    return transactions


# ---------------------------------------------------------------------------
# Category System
# ---------------------------------------------------------------------------

def _fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _fetch_user_category_map(user_id):
    """
    Fetch the user's learned merchant → category mappings from
    `user_category_map` ONE TIME.

    Returns a list of row-like objects (each supporting row["merchant"]
    / row["category"]), or [] on failure / missing user_id.

    Callers that process many transactions in a single request (PDF
    statement parsing, CSV import) call this ONCE up front and pass the
    result into get_smart_category(..., user_map=...) for every
    transaction, instead of letting get_smart_category hit the DB itself
    for each transaction.
    """
    if user_id is None:
        return []

    conn = None
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT merchant, category FROM user_category_map WHERE user_id = %s",
            (user_id,)
        ).fetchall()
        return rows
    except Exception:
        logger.exception("Failed to fetch user_category_map for user_id=%s.", user_id)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_smart_category(user_id, description: str, user_map=None) -> str:
    """
    Smart category detection pipeline (robust + safe):

    1. User-learned mapping (substring + fuzzy)
    2. Global MERCHANT_MAP
    3. Keyword detection
    4. ML prediction
    5. Final fallback → "Misc"

    ALWAYS returns a valid category from ALLOWED_CATEGORIES

    `user_map`: optional pre-fetched result of _fetch_user_category_map().
    When provided, tier 1 uses it directly instead of querying the DB —
    this is what lets batch callers (PDF/CSV import) fetch the mapping
    ONCE per request and reuse it across every transaction. When omitted
    (single-transaction call sites like add-transaction / suggest-category),
    behavior is unchanged: the mapping is fetched here, per call, exactly
    as before.
    """

    # ----------------------------
    # 🧹 NORMALIZE INPUT
    # ----------------------------
    desc_norm = normalize_text(description)
    if not desc_norm:
        return "Misc"

    # ----------------------------
    # 🥇 1. USER LEARNING
    # ----------------------------
    if user_id is not None:
        try:
            # 🔥 Reuse a pre-fetched map when the caller supplied one;
            # otherwise fall back to the original per-call DB fetch so
            # single-transaction callers keep their existing behavior.
            rows = user_map if user_map is not None else _fetch_user_category_map(user_id)

            FUZZY_THRESHOLD = 0.72
            best_ratio = 0.0
            best_cat = None

            for row in rows:
                merchant_norm = normalize_text(row["merchant"])
                if not merchant_norm:
                    continue

                # 🔥 Direct match (fast)
                if merchant_norm in desc_norm or desc_norm in merchant_norm:
                    if row["category"] in ALLOWED_CATEGORIES:
                        return row["category"]

                # 🔥 Fuzzy match
                ratio = _fuzzy_ratio(desc_norm, merchant_norm)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_cat = row["category"]

            if best_cat and best_ratio >= FUZZY_THRESHOLD:
                if best_cat in ALLOWED_CATEGORIES:
                    return best_cat

        except Exception:
            logger.exception("Tier 1 (user learning) failed.")

    # ----------------------------
    # 🥈 2. MERCHANT MAP
    # ----------------------------
    try:
        for key, cat in MERCHANT_MAP.items():
            if normalize_text(key) in desc_norm:
                if cat in ALLOWED_CATEGORIES:
                    return cat
    except Exception:
        logger.exception("Tier 2 (merchant map) failed.")

    # ----------------------------
    # 🥉 3. KEYWORD MATCH
    # ----------------------------
    for cat, words in CATEGORY_KEYWORDS.items():
        if cat not in ALLOWED_CATEGORIES:
            continue
        if any(w in desc_norm for w in words):
            return cat

    # ----------------------------
    # 🧠 4. ML PREDICTION
    # ----------------------------
    try:
        pred = predict_category(description)

        # 🔥 CRITICAL FIX (YOUR BUG WAS HERE)
        if not pred:
            return "Misc"

        pred = pred.strip().title()

        if pred in ALLOWED_CATEGORIES:
            return pred

    except Exception:
        logger.debug("ML prediction failed for '%s'.", description)

    # ----------------------------
    # 🛡️ 5. FINAL FALLBACK
    # ----------------------------
    return "Misc"


# ---------------------------------------------------------------------------
# Internal DB Helper
# ---------------------------------------------------------------------------

def _db_execute(fn):
    """Run fn(conn) with guaranteed commit + close. Returns fn's result."""
    conn = get_db()
    try:
        result = fn(conn)
        conn.commit()
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transaction metadata helpers
# ---------------------------------------------------------------------------

def _parse_transaction_timestamp(raw):
    """Parse an ISO/local timestamp without inventing a time for historical imports."""
    if raw is None or str(raw).strip() == "":
        return None
    value = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # The app and statement parsers send local timestamps without an
        # offset. For offset-aware values, preserve the wall-clock components
        # rather than silently converting to the server's UTC timezone.
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %I:%M %p"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _extract_time(block: str) -> str | None:
    m = _TIME_RE.search(block or "")
    if not m:
        return None
    raw = re.sub(r"\s+", "", m.group(1)).upper()
    for fmt in ("%I:%M:%S%p", "%I:%M%p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _build_timestamp(date_str: str | None, time_str: str | None) -> str | None:
    if not date_str or not time_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        t = datetime.strptime(time_str, "%H:%M:%S").time()
        return datetime.combine(d, t).isoformat()
    except ValueError:
        return None


def _extract_reference_id(block: str) -> str | None:
    patterns = (
        r"(?i)UPI\s+Ref\s+No\.?\s*[:\-]?\s*([A-Z0-9]+)",
        r"(?i)Transaction\s+ID\s*[:\-]?\s*([A-Z0-9]+)",
        r"(?i)Txn\s+ID\s*[:\-]?\s*([A-Z0-9]+)",
        r"(?i)UPITransactionID\s*:\s*([A-Z0-9]+)",
        r"(?i)UTR\s+No\.?\s*[:\-]?\s*([A-Z0-9]+)",
    )
    for pattern in patterns:
        m = re.search(pattern, block or "")
        if m:
            return m.group(1)
    return None


def _extract_utr(block: str) -> str | None:
    m = re.search(r"(?i)UTR\s+No\.?\s*[:\-]?\s*([A-Z0-9]+)", block or "")
    return m.group(1) if m else None


def _format_db_timestamp(value):
    """Return database timestamps in a stable ISO-8601 string for the Flutter client."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _format_db_date(value):
    """Return DATE/DATETIME database values as YYYY-MM-DD, never Flask's RFC-1123 format."""
    if value is None:
        return None
    if hasattr(value, "date") and not isinstance(value, type(value).mro()[-1]):
        # Kept intentionally conservative; datetime/date both expose isoformat.
        pass
    if hasattr(value, "isoformat"):
        raw = value.isoformat()
        return raw[:10]
    text = str(value).strip()
    # Handle legacy Flask-style or other textual dates defensively.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else text


def _transaction_response(row):
    """Serialize a transaction row explicitly so date/time never become Flask RFC-1123 strings."""
    return {
        "id": row.get("id"),
        "description": row.get("description") or "",
        "amount": row.get("amount", 0),
        "type": row.get("type") or "expense",
        "category": row.get("category") or "Misc",
        "date": _format_db_date(row.get("date")) or "",
        "transaction_timestamp": _format_db_timestamp(row.get("transaction_timestamp")),
        "reference_id": row.get("reference_id"),
        "utr": row.get("utr"),
        "source": row.get("source"),
        "status": row.get("status") or "completed",
    }


# ---------------------------------------------------------------------------
# Routes — Transaction CRUD
# ---------------------------------------------------------------------------

@transactions_bp.route("/add-transaction", methods=["POST"])
@login_required
def add_transaction():
    data    = request.get_json(force=True)
    user_id = session["user_id"]

    description = (data.get("description") or "").strip()
    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid amount."}), 400
    t_type      = (data.get("type") or "expense").strip().lower()
    category    = (data.get("category") or "").strip()
    date        = (data.get("date") or "").strip() or datetime.today().strftime("%Y-%m-%d")
    transaction_timestamp = _parse_transaction_timestamp(data.get("transaction_timestamp") or data.get("timestamp"))
    reference_id = (data.get("reference_id") or data.get("referenceId") or "").strip()[:150] or None
    utr = (data.get("utr") or "").strip()[:100] or None
    source = (data.get("source") or "FinTrust").strip()[:100] or "FinTrust"
    if transaction_timestamp is None and date == datetime.today().strftime("%Y-%m-%d"):
        transaction_timestamp = datetime.now().replace(microsecond=0)

    if not description:
        return jsonify({"success": False, "error": "Description is required."}), 400
    if amount <= 0:
        return jsonify({"success": False, "error": "Amount must be positive."}), 400
    if t_type not in ("income", "expense"):
        t_type = "expense"

    # 🔥 AUTO-DETECT FIX
    if not category or category.lower() == "auto-detect":
        try:
            category = get_smart_category(user_id, description)
        except Exception:
            category = "Misc"

# 🔥 ABSOLUTE FINAL SAFETY (THIS WAS MISSING)
    if not category or category not in ALLOWED_CATEGORIES:
        category = "Misc"

    def _insert(conn):
        if reference_id:
            existing = conn.execute(
                "SELECT id FROM transactions WHERE user_id=%s AND reference_id=%s LIMIT 1",
                (user_id, reference_id)
            ).fetchone()
            if existing:
                return existing["id"], True

        cur = conn.execute(
            "INSERT INTO transactions "
            "(user_id, description, amount, type, category, date, transaction_timestamp, reference_id, utr, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (user_id, description, amount, t_type, category, date, transaction_timestamp, reference_id, utr, source)
        )
        return cur.fetchone()["id"], False

    tid, duplicate = _db_execute(_insert)
    return jsonify({"success": True, "id": tid, "duplicate": duplicate})


@transactions_bp.route("/get-transactions")
@login_required
def get_transactions():
    user_id  = session["user_id"]
    start    = request.args.get("start")
    end      = request.args.get("end")
    category = request.args.get("category")
    t_type   = request.args.get("type")
    search   = (request.args.get("search") or "").strip()

    query  = "SELECT * FROM transactions WHERE user_id = %s"
    params: list = [user_id]

    if start:
        query += " AND date >= %s";  params.append(start)
    if end:
        query += " AND date <= %s";  params.append(end)
    if category and category != "All":
        query += " AND category = %s"; params.append(category)
    if t_type and t_type != "All":
        query += " AND type = %s";   params.append(t_type)
    if search:
        query += " AND LOWER(description) LIKE %s"; params.append(f"%{search.lower()}%")

    query += " ORDER BY transaction_timestamp DESC NULLS LAST, date DESC, id DESC"

    conn = get_db()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    return jsonify({"transactions": [_transaction_response(r) for r in rows]})


@transactions_bp.route("/delete-transaction/<int:tid>", methods=["DELETE"])
@login_required
def delete_transaction(tid):
    def _delete(conn):
        cur = conn.execute(
            "DELETE FROM transactions WHERE id = %s AND user_id = %s",
            (tid, session["user_id"])
        )
        return cur.rowcount > 0

    found = _db_execute(_delete)
    if not found:
        return jsonify({"success": False, "error": "Transaction not found."}), 404
    return jsonify({"success": True})


@transactions_bp.route("/update-transaction/<int:tid>", methods=["PUT"])
@login_required
def update_transaction(tid):
    data    = request.get_json(force=True)
    user_id = session["user_id"]

    description = (data.get("description") or "").strip()
    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid amount."}), 400
    category    = (data.get("category") or "Misc").strip()
    t_type      = (data.get("type") or "expense").strip().lower()
    date        = (data.get("date") or "").strip() or datetime.today().strftime("%Y-%m-%d")
    transaction_timestamp = _parse_transaction_timestamp(data.get("transaction_timestamp") or data.get("timestamp"))
    reference_id = (data.get("reference_id") or data.get("referenceId") or "").strip()[:150] or None
    utr = (data.get("utr") or "").strip()[:100] or None
    source = (data.get("source") or "").strip()[:100] or None
    if transaction_timestamp is None and date == datetime.today().strftime("%Y-%m-%d"):
        transaction_timestamp = datetime.now().replace(microsecond=0)

    if not description:
        return jsonify({"success": False, "error": "Description is required."}), 400
    if amount <= 0:
        return jsonify({"success": False, "error": "Amount must be positive."}), 400
    if t_type not in ("income", "expense"):
        t_type = "expense"
    if category not in ALLOWED_CATEGORIES:
        category = "Misc"

    def _update(conn):
        cur = conn.execute(
            "UPDATE transactions "
            "SET description=%s, amount=%s, category=%s, type=%s, date=%s, "
            "transaction_timestamp=%s, reference_id=%s, utr=%s, source=%s "
            "WHERE id=%s AND user_id=%s",
            (description, amount, category, t_type, date, transaction_timestamp, reference_id, utr, source, tid, user_id)
        )
        if cur.rowcount == 0:
            return False
        # Persist user learning for future auto-detection
        conn.execute(
            "INSERT INTO user_category_map (user_id, merchant, category) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, merchant) DO UPDATE SET category = EXCLUDED.category",
            (user_id, normalize_text(description), category)
        )
        return True

    found = _db_execute(_update)
    if not found:
        return jsonify({"success": False, "error": "Transaction not found."}), 404
    return jsonify({"success": True})


@transactions_bp.route("/clear-all-transactions", methods=["POST"])
@login_required
def clear_all_transactions():
    _db_execute(lambda conn: conn.execute(
        "DELETE FROM transactions WHERE user_id = %s", (session["user_id"],)
    ))
    return jsonify({"success": True})


@transactions_bp.route("/suggest-category")
@login_required
def suggest_category():
    """
    GET /suggest-category?description=...
    Lightweight endpoint for the frontend live auto-detect hint.
    Returns { category: str } using the full get_smart_category pipeline.
    """
    description = (request.args.get("description") or "").strip()
    if not description:
        return jsonify({"category": "Misc"})
    try:
        category = get_smart_category(session["user_id"], description)
    except Exception:
        category = "Misc"
    return jsonify({"category": category})


# ---------------------------------------------------------------------------
# Routes — CSV Export / Import
# ---------------------------------------------------------------------------

@transactions_bp.route("/export-transactions")
@login_required
def export_transactions():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT description, amount, type, category, date, transaction_timestamp, reference_id, utr, source "
            "FROM transactions WHERE user_id = %s ORDER BY transaction_timestamp DESC NULLS LAST, date DESC, id DESC",
            (session["user_id"],)
        ).fetchall()
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["description", "amount", "type", "category", "date", "time", "transaction_timestamp", "reference_id", "utr", "source"])
    for r in rows:
        ts = _format_db_timestamp(r.get("transaction_timestamp"))
        time_value = ts[11:19] if ts and len(ts) >= 19 else ""
        writer.writerow([r["description"], r["amount"], r["type"], r["category"] or "Misc", r["date"], time_value, ts or "", r.get("reference_id") or "", r.get("utr") or "", r.get("source") or ""])

    # UTF-8 BOM so Excel opens it correctly
    csv_bytes = ("\ufeff" + output.getvalue()).encode("utf-8")
    response = make_response(csv_bytes)
    response.headers["Content-Disposition"] = "attachment; filename=transactions.csv"
    response.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    return response


@transactions_bp.route("/import-transactions", methods=["POST"])
@login_required
def import_transactions():
    user_id = session["user_id"]

    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"success": False, "error": "No file uploaded."}), 400

    try:
        raw_bytes = request.files["file"].stream.read()
        try:
            stream = io.StringIO(raw_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            stream = io.StringIO(raw_bytes.decode("latin-1"))
        reader   = csv.DictReader(stream)
        conn     = get_db()
        inserted = 0

        # 🔥 PERFORMANCE FIX: fetch user_category_map ONCE for this whole
        # CSV import instead of once per row inside get_smart_category().
        user_map = _fetch_user_category_map(user_id)

        try:
            for row in reader:
                description = (row.get("description") or "").strip()
                amount      = float(row.get("amount") or 0)
                t_type      = (row.get("type") or "expense").strip().lower()
                if t_type not in ("income", "expense"):
                    t_type = "expense"
                category    = (row.get("category") or "").strip()
                date        = (row.get("date") or "").strip() or datetime.today().strftime("%Y-%m-%d")
                transaction_timestamp = _parse_transaction_timestamp(row.get("transaction_timestamp") or row.get("timestamp"))
                if transaction_timestamp is None:
                    csv_time = (row.get("time") or "").strip()
                    transaction_timestamp = _parse_transaction_timestamp(f"{date} {csv_time}") if csv_time else None
                reference_id = (row.get("reference_id") or row.get("referenceId") or "").strip()[:150] or None
                utr = (row.get("utr") or "").strip()[:100] or None
                source = (row.get("source") or "CSV import").strip()[:100]

                if not description or amount <= 0:
                    continue

                if not category or category.lower() == "auto-detect":
                    try:
                        category = get_smart_category(user_id, description, user_map=user_map)

                    except Exception:
                        category = "Misc"

                if not category or category not in ALLOWED_CATEGORIES:
                    category = "Misc"

                conn.execute(
                    "INSERT INTO transactions "
                    "(user_id, description, amount, type, category, date, transaction_timestamp, reference_id, utr, source) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (user_id, description, amount, t_type, category, date, transaction_timestamp, reference_id, utr, source)
                )
                inserted += 1

            conn.commit()
        finally:
            conn.close()

        return jsonify({"success": True, "imported": inserted})

    except Exception as e:
        logger.exception("import_transactions failed.")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Shared PDF extraction helper
# ---------------------------------------------------------------------------

def _extract_pdf_text(file) -> str:
    import pdfplumber

    text = ""

    with pdfplumber.open(file) as pdf:
        for i, page in enumerate(pdf.pages):

            # 🔥 LIMIT pages (CRITICAL FIX)
            if i >= 20:
                break

            # 🔥 FAST extraction
            page_text = page.extract_text(layout=False)

            if page_text:
                text += page_text + "\n"

    return text


# ---------------------------------------------------------------------------
# Routes — PDF Statement Upload
# ---------------------------------------------------------------------------

@transactions_bp.route("/upload-statement", methods=["POST"])
@login_required
def upload_statement():
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "error": "No file provided."}), 400

    try:
        text = _extract_pdf_text(file)
        transactions = parse_upi_statement(text, session["user_id"])
        
        if not transactions:
            return jsonify({"success": False, "error": "No transactions found in statement."}), 422

        user_id = session["user_id"]
        conn    = get_db()
        try:
            for t in transactions:
                conn.execute(
                    "INSERT INTO transactions "
                    "(user_id, description, amount, type, category, date, transaction_timestamp, reference_id, utr, source) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (user_id, t["description"], t["amount"], t["type"], t["category"], t["date"], t.get("transaction_timestamp"), t.get("reference_id"), t.get("utr"), t.get("source", "Statement"))
                )
            conn.commit()
        finally:
            conn.close()

        return jsonify({"success": True, "count": len(transactions)})

    except Exception as e:
        logger.exception("upload_statement failed.")
        return jsonify({"success": False, "error": str(e)}), 500


@transactions_bp.route("/upload-statement-preview", methods=["POST"])
@login_required
def upload_statement_preview():
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "error": "No file provided."}), 400

    try:
        text = _extract_pdf_text(file)
        if not text.strip():
            return jsonify({"success": False, "error": "No readable text found in PDF."}), 422

        transactions = parse_upi_statement(text, session["user_id"])
        return jsonify({"success": True, "transactions": transactions})

    except Exception as e:
        logger.exception("upload_statement_preview failed.")
        return jsonify({"success": False, "error": str(e)}), 500


@transactions_bp.route("/save-preview-transactions", methods=["POST"])
@login_required
def save_preview_transactions():
    data    = request.get_json(force=True)
    txs     = data.get("transactions") or []
    user_id = session["user_id"]

    if not txs:
        return jsonify({"success": False, "error": "No transactions provided."}), 400

    conn     = get_db()
    inserted = 0
    try:
        for t in txs:
            description = (t.get("description") or "").strip()
            amount      = float(t.get("amount") or 0)
            t_type      = (t.get("type") or "expense").strip().lower()
            category    = (t.get("category") or "Misc").strip()
            date        = (t.get("date") or "").strip() or datetime.today().strftime("%Y-%m-%d")

            if not description or amount <= 0:
                continue
            if t_type not in ("income", "expense"):
                t_type = "expense"
            if category not in ALLOWED_CATEGORIES:
                category = "Misc"

            # NOTE: relies on a UNIQUE constraint on transactions
            # (user_id, description, amount, type, date) to dedupe,
            # matching the previous SQLite "INSERT OR IGNORE" behavior.
            transaction_timestamp = _parse_transaction_timestamp(t.get("transaction_timestamp") or t.get("timestamp"))
            reference_id = (t.get("reference_id") or t.get("referenceId") or "").strip()[:150] or None
            utr = (t.get("utr") or "").strip()[:100] or None
            source = (t.get("source") or "Statement preview").strip()[:100]
            cur = conn.execute(
                "INSERT INTO transactions "
                "(user_id, description, amount, type, category, date, transaction_timestamp, reference_id, utr, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (user_id, description, amount, t_type, category, date, transaction_timestamp, reference_id, utr, source)
            )
            inserted += cur.rowcount

        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True, "count": inserted})


_BULK_INSERT_COLUMNS = ("user_id", "description", "amount", "type", "category", "date", "transaction_timestamp", "reference_id", "utr", "source")

# PostgreSQL has a hard limit of 65535 bound parameters per statement.
# 6 params/row → cap comfortably below that so even a single INSERT
# statement never risks hitting the limit; a batch larger than this is
# split into a small number of large statements (NOT per-row) within
# the same transaction/commit.
_BULK_INSERT_CHUNK_ROWS = 5000


def _build_bulk_insert_query(n_rows: int):
    """
    Build ONE parameterized "INSERT ... VALUES (%s,...), (%s,...), ...
    ON CONFLICT DO NOTHING RETURNING id" statement for `n_rows` rows,
    using psycopg's sql.SQL/sql.Placeholder composition so structure
    (column names, row count) is composed safely and no user data is
    ever interpolated into the SQL text — only %s placeholders carry
    values, exactly like every other query in this file.
    """
    row_placeholders = sql.SQL("({})").format(
        sql.SQL(", ").join([sql.Placeholder()] * len(_BULK_INSERT_COLUMNS))
    )
    values_clause = sql.SQL(", ").join([row_placeholders] * n_rows)

    return sql.SQL(
        "INSERT INTO transactions ({columns}) "
        "VALUES {values} "
        "ON CONFLICT DO NOTHING "
        "RETURNING id"
    ).format(
        columns=sql.SQL(", ").join(sql.Identifier(c) for c in _BULK_INSERT_COLUMNS),
        values=values_clause,
    )


def _bulk_insert_transactions(conn, rows):
    """
    Insert `rows` — tuples of
    (user_id, description, amount, type, category, date) — as ONE real
    PostgreSQL bulk INSERT statement (chunked only if the batch is
    large enough to risk the parameter-count limit), and report which
    ones were actually inserted (vs. skipped as duplicates).

    WHY THIS EXISTS (root-cause note):
    The app runs on psycopg 3 (via utils/db.py), not psycopg2. An
    earlier version of this function tried to import
    `psycopg2.extras.execute_values()` for the fast path — that import
    always fails here (psycopg2 isn't installed/used), so every import
    silently fell through to a per-row `conn.execute()` loop: one
    round trip AND one implicit statement per transaction. For a
    500-row import that's ~500 sequential network round trips to
    Postgres, which is exactly where the ~20s went. Wrapping that loop
    in a single commit() never fixed it, because the cost was in the
    N round trips during INSERT, not in the number of commits.

    FIX: build a single "INSERT ... VALUES (r1), (r2), ..., (rN)"
    statement (psycopg 3 natively supports this — no special bulk API
    needed) and send it in ONE conn.execute() call. That's O(1) round
    trips instead of O(n). All values are still passed as bound
    parameters (never string-interpolated) via psycopg's sql.SQL /
    sql.Placeholder composition.

    ON CONFLICT DO NOTHING preserves the same duplicate-skip semantics
    already used by /save-preview-transactions, relying on the
    existing UNIQUE constraint on (user_id, description, amount, type,
    date) — no per-row SELECT is needed to detect duplicates.

    Returns: list of ids that were actually inserted (duplicates
    excluded via RETURNING), so the caller can report an accurate
    imported count. There is no fallback path — if the bulk statement
    fails, the exception propagates so the caller can roll back the
    whole batch (see import_preview_transactions).
    """
    inserted_ids = []

    for start in range(0, len(rows), _BULK_INSERT_CHUNK_ROWS):
        chunk = rows[start:start + _BULK_INSERT_CHUNK_ROWS]

        query = _build_bulk_insert_query(len(chunk))
        flat_params = [value for row in chunk for value in row]

        # ONE statement for the whole chunk — not one execute() per row.
        cur = conn.execute(query, flat_params)
        inserted_ids.extend(r["id"] for r in cur.fetchall())

    return inserted_ids


@transactions_bp.route("/import-preview-transactions", methods=["POST"])
@login_required
def import_preview_transactions():
    """
    Bulk-insert the transactions the user has already reviewed/edited in
    the PDF/CSV preview modal.

    This endpoint receives ONLY the previewData JSON produced by
    /upload-statement-preview (as edited by the user via editField() /
    removeRow()) — it never receives or re-parses the original PDF, and
    it never re-runs categorization. Whatever category/description/
    amount/date/type is in each row is exactly what gets inserted.

    Request body:
        { "transactions": [ { description, amount, type, category, date }, ... ] }

    All valid rows are inserted in a single DB transaction (one
    round-trip-efficient bulk statement, see _bulk_insert_transactions);
    if it fails, everything is rolled back and the preview modal should
    stay open so the user can retry. Duplicates (matching an existing
    row on user_id/description/amount/type/date) are silently skipped
    via ON CONFLICT DO NOTHING, same as /save-preview-transactions.
    """
    t_start = time.perf_counter()

    data    = request.get_json(force=True) or {}
    txs     = data.get("transactions") or []
    user_id = session["user_id"]
    t_json  = time.perf_counter()

    if not isinstance(txs, list) or not txs:
        return jsonify({"success": False, "error": "No transactions provided."}), 400

    today = datetime.today().strftime("%Y-%m-%d")
    valid_rows = []

    for t in txs:
        if not isinstance(t, dict):
            continue

        description = (t.get("description") or "").strip()

        try:
            amount = float(t.get("amount") or 0)
        except (ValueError, TypeError):
            continue

        t_type = (t.get("type") or "expense").strip().lower()
        if t_type not in ("income", "expense"):
            t_type = "expense"

        category = (t.get("category") or "Misc").strip()
        if category not in ALLOWED_CATEGORIES:
            category = "Misc"

        date = (t.get("date") or "").strip() or today

        # Preserve existing validation rules (same as save-preview-transactions
        # / import-transactions): non-empty description, positive amount.
        if not description or amount <= 0:
            continue

        transaction_timestamp = _parse_transaction_timestamp(t.get("transaction_timestamp") or t.get("timestamp"))
        reference_id = (t.get("reference_id") or t.get("referenceId") or "").strip()[:150] or None
        utr = (t.get("utr") or "").strip()[:100] or None
        source = (t.get("source") or "Statement import").strip()[:100]

        valid_rows.append((user_id, description, amount, t_type, category, date, transaction_timestamp, reference_id, utr, source))

    t_validated = time.perf_counter()

    if not valid_rows:
        return jsonify({"success": False, "error": "No valid transactions to import."}), 400

    conn = get_db()
    t_conn = time.perf_counter()

    try:
        inserted_ids = _bulk_insert_transactions(conn, valid_rows)
        t_insert = time.perf_counter()

        conn.commit()
        t_commit = time.perf_counter()

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("import_preview_transactions bulk insert failed.")
        return jsonify({
            "success": False,
            "error": "Import failed. Nothing was saved — please try again."
        }), 500
    finally:
        conn.close()

    t_end = time.perf_counter()

    imported = len(inserted_ids)
    skipped_duplicates = len(valid_rows) - imported

    if IMPORT_PROFILE:
        logger.info(
            "[IMPORT] json_parse=%.4fs validate=%.4fs db_connect=%.4fs "
            "bulk_insert=%.4fs commit=%.4fs total=%.4fs rows_submitted=%d "
            "rows_inserted=%d rows_duplicate=%d",
            t_json - t_start, t_validated - t_json, t_conn - t_validated,
            t_insert - t_conn, t_commit - t_insert, t_end - t_start,
            len(valid_rows), imported, skipped_duplicates
        )
    else:
        # Concise, production-safe perf log (always on).
        logger.info(
            "[IMPORT] imported=%d duplicates=%d total=%.3fs",
            imported, skipped_duplicates, t_end - t_start
        )

    return jsonify({
        "success": True,
        "imported": imported,
        "skipped_duplicates": skipped_duplicates
    })