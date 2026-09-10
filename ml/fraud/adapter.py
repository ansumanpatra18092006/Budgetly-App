"""
===============================================================
FraudShield - FinTrust Transaction Adapter
===============================================================

Converts FinTrust's native transaction structure into the
normalized transaction representation expected by FraudShield.

FinTrust transaction example:

{
    "amount": 25000,
    "type": "debit",
    "category": "UPI",
    "description": "UPI PAYMENT",
    "date": "2026-09-10",
    "transaction_timestamp": "2026-09-10 14:30:00",
    "reference_id": "TXN123",
    "utr": "123456789",
    "source": "UPI"
}

The adapter does NOT calculate the final fraud score.

It only normalizes the transaction so subsequent FraudShield
components can analyze it.

===============================================================
"""

from datetime import datetime
import math
import re


# ===============================================================
# PRODUCT CODE MAPPING
# ===============================================================

CATEGORY_TO_PRODUCT = {
    "upi": "W",
    "online": "W",
    "ecommerce": "W",
    "shopping": "W",
    "card": "W",
    "debit card": "W",
    "credit card": "W",

    "atm": "C",
    "cash": "C",

    "bank transfer": "R",
    "transfer": "R",
    "neft": "R",
    "rtgs": "R",
    "imps": "R",

    "payment": "W",
}


# ===============================================================
# TRANSACTION TYPE NORMALIZATION
# ===============================================================

def normalize_type(value):

    if value is None:
        return "unknown"

    value = str(value).strip().lower()

    if value in {
        "debit",
        "dr",
        "withdrawal",
        "payment"
    }:
        return "debit"

    if value in {
        "credit",
        "cr",
        "deposit",
        "refund"
    }:
        return "credit"

    return value


# ===============================================================
# CATEGORY NORMALIZATION
# ===============================================================

def normalize_category(value):

    if value is None:
        return "unknown"

    return (
        str(value)
        .strip()
        .lower()
    )


# ===============================================================
# AMOUNT NORMALIZATION
# ===============================================================

def normalize_amount(value):

    if value is None:
        return 0.0

    try:

        amount = float(value)

        if not math.isfinite(amount):
            return 0.0

        return abs(amount)

    except (
        TypeError,
        ValueError
    ):

        return 0.0


# ===============================================================
# TIMESTAMP PARSER
# ===============================================================

def parse_timestamp(transaction):

    timestamp = (
        transaction.get(
            "transaction_timestamp"
        )
        or
        transaction.get(
            "date"
        )
    )

    if timestamp is None:

        return datetime.now()

    # Already datetime
    if isinstance(
        timestamp,
        datetime
    ):

        return timestamp

    timestamp = str(
        timestamp
    ).strip()

    formats = [

        "%Y-%m-%d %H:%M:%S",

        "%Y-%m-%d %H:%M",

        "%Y-%m-%dT%H:%M:%S",

        "%Y-%m-%dT%H:%M",

        "%d-%m-%Y %H:%M:%S",

        "%d-%m-%Y %H:%M",

        "%d/%m/%Y %H:%M:%S",

        "%d/%m/%Y %H:%M",

        "%Y-%m-%d",
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                timestamp,
                fmt
            )

        except ValueError:

            continue

    return datetime.now()


# ===============================================================
# TRANSACTION TIME → IEEE-CIS STYLE TIMESTAMP
# ===============================================================

def create_transaction_dt(timestamp):

    """
    IEEE-CIS TransactionDT is a relative time value.

    For FinTrust we create a deterministic relative-second
    representation from the Unix epoch.

    This is only a compatibility representation.
    """

    return int(
        timestamp.timestamp()
    )


# ===============================================================
# DESCRIPTION ANALYSIS
# ===============================================================

def analyze_description(
    description
):

    if description is None:

        description = ""

    description = str(
        description
    ).lower()

    suspicious_keywords = [

        "urgent",
        "verify",
        "verification",
        "blocked",
        "unblock",
        "refund",
        "reward",
        "lottery",
        "winner",
        "prize",
        "cashback",
        "claim",
        "kyc",
        "otp",
        "password",
        "security",
        "account",
    ]

    matched = [

        keyword
        for keyword in suspicious_keywords
        if keyword in description
    ]

    return {

        "description_length":
            len(description),

        "description_has_url":
            int(
                bool(
                    re.search(
                        r"https?://|www\.",
                        description
                    )
                )
            ),

        "suspicious_keyword_count":
            len(matched),

        "suspicious_keywords":
            matched,
    }


# ===============================================================
# PRODUCT CODE
# ===============================================================

def determine_product_code(
    transaction
):

    category = normalize_category(
        transaction.get(
            "category"
        )
    )

    source = normalize_category(
        transaction.get(
            "source"
        )
    )

    combined = (
        f"{category} {source}"
    )

    for keyword, code in (
        CATEGORY_TO_PRODUCT.items()
    ):

        if keyword in combined:

            return code

    return "W"


# ===============================================================
# MAIN ADAPTER
# ===============================================================

def adapt_fintrust_transaction(
    transaction
):
    """
    Convert a FinTrust transaction into a normalized
    FraudShield transaction representation.
    """

    if not isinstance(
        transaction,
        dict
    ):

        raise TypeError(
            "Transaction must be a dictionary."
        )


    # -----------------------------------------------------------
    # Basic fields
    # -----------------------------------------------------------

    amount = normalize_amount(
        transaction.get(
            "amount",
            transaction.get(
                "TransactionAmt",
                0
            )
        )
    )

    transaction_type = normalize_type(
        transaction.get(
            "type"
        )
    )

    category = normalize_category(
        transaction.get(
            "category"
        )
    )

    description = transaction.get(
        "description",
        ""
    )

    source = normalize_category(
        transaction.get(
            "source"
        )
    )


    # -----------------------------------------------------------
    # Timestamp
    # -----------------------------------------------------------

    timestamp = parse_timestamp(
        transaction
    )

    transaction_dt = (
        create_transaction_dt(
            timestamp
        )
    )


    # -----------------------------------------------------------
    # Description signals
    # -----------------------------------------------------------

    description_features = (
        analyze_description(
            description
        )
    )


    # -----------------------------------------------------------
    # Product code
    # -----------------------------------------------------------

    product_code = (
        determine_product_code(
            transaction
        )
    )


    # -----------------------------------------------------------
    # Build normalized transaction
    # -----------------------------------------------------------

    adapted = {

        # Original FinTrust values
        "fintrust_amount":
            amount,

        "fintrust_type":
            transaction_type,

        "fintrust_category":
            category,

        "fintrust_description":
            description,

        "fintrust_source":
            source,

        "reference_id":
            transaction.get(
                "reference_id"
            ),

        "utr":
            transaction.get(
                "utr"
            ),

        # FraudShield-compatible values
        "TransactionAmt":
            amount,

        "TransactionDT":
            transaction_dt,

        "ProductCD":
            product_code,

        # -----------------------------------------------------
        # Optional IEEE-CIS-compatible fields
        #
        # These remain unknown unless FinTrust actually has
        # equivalent information.
        # -----------------------------------------------------

        "card1":
            transaction.get(
                "card1"
            ),

        "card2":
            transaction.get(
                "card2"
            ),

        "card3":
            transaction.get(
                "card3"
            ),

        "card4":
            transaction.get(
                "card4"
            ),

        "card5":
            transaction.get(
                "card5"
            ),

        "card6":
            transaction.get(
                "card6"
            ),

        "addr1":
            transaction.get(
                "addr1"
            ),

        "addr2":
            transaction.get(
                "addr2"
            ),

        "dist1":
            transaction.get(
                "dist1"
            ),

        "dist2":
            transaction.get(
                "dist2"
            ),

        "P_emaildomain":
            transaction.get(
                "P_emaildomain"
            ),

        "R_emaildomain":
            transaction.get(
                "R_emaildomain"
            ),

        "DeviceType":
            transaction.get(
                "DeviceType"
            ),

        "DeviceInfo":
            transaction.get(
                "DeviceInfo"
            ),

        # Description-derived signals
        **description_features,

        # Timestamp information
        "transaction_timestamp":
            timestamp.isoformat(),

        "transaction_hour":
            timestamp.hour,

        "transaction_weekday":
            timestamp.weekday(),

        "is_night_transaction":
            int(
                timestamp.hour < 6
                or
                timestamp.hour >= 22
            ),
    }


    return adapted


# ===============================================================
# SIMPLE PUBLIC FUNCTION
# ===============================================================

def adapt_transaction(
    transaction
):

    return adapt_fintrust_transaction(
        transaction
    )


# ===============================================================
# TEST
# ===============================================================

if __name__ == "__main__":

    print("=" * 70)
    print("FINTRUST → FRAUDSHIELD ADAPTER TEST")
    print("=" * 70)

    sample_transaction = {

        "amount": 25000,

        "type": "debit",

        "category": "UPI",

        "description":
            "UPI PAYMENT",

        "date":
            "2026-09-10",

        "transaction_timestamp":
            "2026-09-10 14:30:00",

        "reference_id":
            "TXN-TEST-001",

        "utr":
            "123456789012",

        "source":
            "UPI",
    }


    result = adapt_fintrust_transaction(
        sample_transaction
    )


    print()
    print("Original transaction:")
    print(sample_transaction)

    print()
    print("Adapted transaction:")

    for key, value in result.items():

        print(
            f"{key:35} : {value}"
        )

    print()
    print("=" * 70)
    print("ADAPTER TEST COMPLETED")
    print("=" * 70)