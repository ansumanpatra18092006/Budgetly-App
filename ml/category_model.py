import pickle
import os
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

MODEL_PATH = "ml/category.pkl"

vectorizer = None
model = None

ALLOWED_CATEGORIES = {
    "Income", "Food", "Utilities", "Transport", "Rent", "Shopping",
    "Health", "Education", "Entertainment", "Subscriptions",
    "Travel", "Personal", "Family", "Finance", "Insurance", "Misc"
}

CONFIDENCE_THRESHOLD = 0.35

# ====================================
# RULE-BASED OVERRIDES
# Order matters: more specific rules first.
# Each entry: (keyword_list, category)
# ====================================
RULES = [
    # ── Income ──────────────────────────────────────────────────────────
    (["salary", "paycheck", "pay check", "stipend", "bonus received",
      "bonus credited", "income credited", "salary credited",
      "salary received", "freelance payment", "freelance income",
      "refund received", "cashback received", "cashback credited",
      "interest credit", "interest credited", "dividend", "incentive",
      "reimbursement", "payout"], "Income"),

    # ── Food ────────────────────────────────────────────────────────────
    (["swiggy", "zomato", "blinkit", "dunzo", "bigbasket",
      "restaurant", "cafe", "diner", "food court", "dhaba",
      "grocery", "kirana", "supermarket", "provision store",
      "bakery", "dairy", "milk delivery"], "Food"),

    # ── Transport ───────────────────────────────────────────────────────
    (["uber", "ola cab", "rapido", "taxi", "auto fare",
      "bus ticket", "metro card", "metro recharge",
      "petrol", "diesel", "cng", "fuel", "toll plaza",
      "fastag", "parking fee", "vehicle service",
      "car repair", "bike repair", "mechanic"], "Transport"),

    # ── Subscriptions ───────────────────────────────────────────────────
    (["netflix", "spotify", "amazon prime", "prime video",
      "hotstar", "disney+", "zee5", "sonyliv",
      "youtube premium", "apple music", "subscription plan",
      "monthly plan", "annual plan", "saas", "software subscription"], "Subscriptions"),

    # ── Shopping ────────────────────────────────────────────────────────
    (["amazon", "flipkart", "myntra", "meesho", "ajio",
      "nykaa", "tata cliq", "snapdeal", "shopclues",
      "online order", "online shopping"], "Shopping"),

    # ── Utilities ───────────────────────────────────────────────────────
    (["electricity bill", "power bill", "water bill",
      "wifi bill", "broadband", "internet bill",
      "mobile recharge", "data recharge", "dth recharge",
      "gas bill", "lpg cylinder", "sewage bill"], "Utilities"),

    # ── Rent ────────────────────────────────────────────────────────────
    (["house rent", "flat rent", "pg rent",
      "room rent", "lease payment", "rent paid"], "Rent"),

    # ── Health ──────────────────────────────────────────────────────────
    (["doctor visit", "clinic", "hospital bill",
      "pharmacy", "medical store", "medicine purchase",
      "blood test", "diagnostic", "lab test", "xray",
      "health checkup", "covid test"], "Health"),

    # ── Education ───────────────────────────────────────────────────────
    (["tuition fee", "course fee", "coaching fee",
      "college fee", "school fee", "exam fee",
      "udemy", "coursera", "study material"], "Education"),

    # ── Travel ──────────────────────────────────────────────────────────
    (["flight ticket", "air ticket", "irctc",
      "hotel booking", "hostel booking", "oyo",
      "tour package", "vacation", "trip booking"], "Travel"),

    # ── Entertainment ───────────────────────────────────────────────────
    (["movie ticket", "cinema", "pvr", "inox",
      "game purchase", "steam", "playstation", "xbox",
      "concert ticket", "event ticket"], "Entertainment"),

    # ── Personal ────────────────────────────────────────────────────────
    (["salon", "haircut", "spa", "massage",
      "gym membership", "fitness", "cosmetics",
      "skincare", "personal care"], "Personal"),

    # ── Family ──────────────────────────────────────────────────────────
    (["gift purchase", "family expense", "toys",
      "kids items", "school supplies", "pet food",
      "vet bill", "pet care"], "Family"),

    # ── Finance ─────────────────────────────────────────────────────────
    (["emi payment", "loan payment", "credit card bill",
      "mutual fund", "sip", "stock purchase", "trading",
      "income tax", "tds", "gst payment",
      "investment", "fd deposit"], "Finance"),

    # ── Insurance ───────────────────────────────────────────────────────
    (["insurance premium", "policy payment",
      "life insurance", "health insurance",
      "vehicle insurance", "term plan"], "Insurance"),
]


# ====================================
# TRAIN FUNCTION
# ====================================
def train_category_model(data):
    """
    data format:
    [{"description": "...", "category": "..."}, ...]
    """
    texts = [d["description"] for d in data]
    labels = [d["category"] for d in data]

    vec = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1, 2),   # bigrams improve phrases like "salary credited"
        min_df=1,
    )
    X = vec.fit_transform(texts)

    clf = LogisticRegression(max_iter=1000, C=5.0, solver="lbfgs")
    clf.fit(X, labels)

    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump((vec, clf), f)

    print(f"[category_model] Model trained on {len(texts)} samples → {MODEL_PATH}")


# ====================================
# LOAD MODEL
# ====================================
def load_model():
    global vectorizer, model
    if vectorizer is None and os.path.exists(MODEL_PATH):
        with open(MODEL_PATH, "rb") as f:
            vectorizer, model = pickle.load(f)


# ====================================
# RULE-BASED OVERRIDE
# ====================================
def _apply_rules(text_lower: str):
    """
    Iterate RULES and return the first matching category.
    Returns None if no rule matches.
    """
    for keywords, category in RULES:
        for kw in keywords:
            if kw in text_lower:
                return category
    return None


# ====================================
# PREDICT
# ====================================
def predict_category(text: str) -> str:
    if not text or not text.strip():
        return "Misc"

    text_lower = text.lower().strip()

    # ── Step 1: Rule-based override ─────────────────────────────────────
    rule_result = _apply_rules(text_lower)
    if rule_result:
        return rule_result

    # ── Step 2: ML prediction ────────────────────────────────────────────
    load_model()

    if vectorizer is None or model is None:
        return "Misc"

    X = vectorizer.transform([text])
    proba = model.predict_proba(X)[0]
    max_confidence = proba.max()

    # ── Step 3: Confidence safety ────────────────────────────────────────
    if max_confidence < CONFIDENCE_THRESHOLD:
        return "Misc"

    pred = model.classes_[proba.argmax()]

    # ── Step 4: Allowed-category guard ───────────────────────────────────
    if pred not in ALLOWED_CATEGORIES:
        return "Misc"

    return pred