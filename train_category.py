import random
from ml.category_model import train_category_model

random.seed(42)

# =====================================================
# Final categories  (must match frontend + backend)
# =====================================================
FINAL_CATEGORIES = [
    "Income",
    "Food",
    "Transport",
    "Rent",
    "Utilities",
    "Shopping",
    "Health",
    "Education",
    "Entertainment",
    "Subscriptions",
    "Travel",
    "Personal",
    "Family",
    "Finance",
    "Insurance",
    "Misc",
]

# =====================================================
# Keywords per category
# =====================================================
category_keywords = {

    # ── Income ───────────────────────────────────────────────────────────
    "Income": [
        "salary", "salary credit", "salary received", "salary transferred",
        "paycheck", "pay check", "monthly salary", "weekly paycheck",
        "stipend", "stipend credited", "internship stipend",
        "bonus", "performance bonus", "annual bonus", "bonus received",
        "freelance payment", "freelance income", "freelance project",
        "refund", "refund received", "order refund", "cashback",
        "cashback credited", "cashback received",
        "interest credit", "savings interest", "fd interest",
        "dividend", "dividend credited", "stock dividend",
        "incentive", "sales incentive", "commission",
        "reimbursement", "expense reimbursement",
        "payout", "withdrawal payout", "prize money",
        "rental income", "rent received", "consulting fee",
    ],

    # ── Food ─────────────────────────────────────────────────────────────
    "Food": [
        "swiggy", "zomato", "blinkit", "dunzo", "bigbasket",
        "restaurant", "cafe", "diner", "food court", "dhaba",
        "pizza hut", "dominos", "mcdonalds", "burger king", "kfc",
        "subway", "starbucks", "chaayos", "haldirams",
        "grocery", "kirana store", "provision store",
        "supermarket", "dmart", "reliance fresh", "more retail",
        "bakery", "confectionery",
        "milk", "milk delivery", "dairy",
        "vegetables", "fruits", "meat", "seafood",
        "rice", "atta", "flour", "pulses", "snacks",
    ],

    # ── Transport ────────────────────────────────────────────────────────
    "Transport": [
        "uber", "uber ride", "ola", "ola cab", "rapido", "meru cab",
        "taxi", "taxi fare", "cab ride", "cab fare",
        "auto", "auto fare", "auto rickshaw",
        "bus", "bus ticket", "city bus", "state bus",
        "metro", "metro card", "metro recharge", "metro fare",
        "train ticket", "local train", "railway ticket",
        "petrol", "diesel", "cng", "fuel", "fuel refill",
        "toll", "toll plaza", "fastag", "toll charge",
        "parking", "parking fee", "parking charge",
        "vehicle service", "car service", "bike service",
        "car repair", "bike repair", "mechanic",
        "puncture repair", "oil change", "tyre change",
    ],

    # ── Rent ─────────────────────────────────────────────────────────────
    "Rent": [
        "house rent", "flat rent", "apartment rent",
        "pg rent", "paying guest rent", "hostel rent",
        "room rent", "room rental",
        "lease payment", "lease rent",
        "rent paid", "monthly rent", "rent transfer",
        "commercial rent", "office rent", "shop rent",
    ],

    # ── Utilities ────────────────────────────────────────────────────────
    "Utilities": [
        "electricity bill", "power bill", "bescom", "tata power",
        "water bill", "water charge", "sewage bill",
        "wifi", "wifi bill", "broadband", "internet bill",
        "jio fiber", "airtel fiber", "bsnl broadband",
        "mobile recharge", "prepaid recharge", "postpaid bill",
        "data recharge", "jio recharge", "airtel recharge",
        "dth recharge", "tata sky", "dish tv",
        "gas bill", "lpg cylinder", "indane gas", "hp gas",
        "piped gas", "mahanagar gas",
    ],

    # ── Shopping ─────────────────────────────────────────────────────────
    "Shopping": [
        "amazon", "amazon order", "amazon purchase",
        "flipkart", "flipkart order",
        "myntra", "myntra order",
        "meesho", "ajio", "nykaa", "tata cliq",
        "online order", "online shopping", "online purchase",
        "laptop", "mobile phone", "tablet", "smartwatch",
        "electronics", "headphones", "earbuds", "speaker",
        "clothes", "clothing", "t-shirt", "jeans", "kurta",
        "shoes", "footwear", "sandals", "sneakers",
        "furniture", "sofa", "bed", "wardrobe",
        "home decor", "curtains", "bedsheet",
        "kitchen appliance", "mixer", "pressure cooker",
    ],

    # ── Health ───────────────────────────────────────────────────────────
    "Health": [
        "doctor visit", "doctor fee", "doctor consultation",
        "clinic", "hospital", "hospital bill",
        "pharmacy", "medical store", "medicine", "tablets",
        "diagnostic", "blood test", "urine test",
        "xray", "mri scan", "ct scan",
        "health checkup", "annual checkup", "lab test",
        "physiotherapy", "dental", "eye checkup",
        "vaccination", "injection", "covid test",
        "apollo pharmacy", "medplus", "1mg", "practo",
    ],

    # ── Education ────────────────────────────────────────────────────────
    "Education": [
        "tuition fee", "course fee", "coaching fee",
        "college fee", "school fee", "semester fee",
        "exam fee", "registration fee",
        "books", "study material", "stationery",
        "online course", "udemy", "coursera", "unacademy",
        "byju", "vedantu", "whitehat jr",
        "library fee", "hostel fee", "lab fee",
    ],

    # ── Entertainment ────────────────────────────────────────────────────
    "Entertainment": [
        "movie ticket", "cinema", "pvr", "inox", "cinepolis",
        "book my show", "event ticket", "concert ticket",
        "game purchase", "gaming", "steam", "epic games",
        "playstation store", "xbox game", "nintendo",
        "amusement park", "theme park", "waterpark",
        "bowling", "paintball", "escape room",
        "stand up comedy", "live show",
    ],

    # ── Subscriptions ────────────────────────────────────────────────────
    "Subscriptions": [
        "netflix", "netflix subscription",
        "spotify", "spotify premium",
        "amazon prime", "prime membership",
        "hotstar", "disney hotstar", "zee5",
        "sonyliv", "voot", "altbalaji",
        "youtube premium", "apple music", "apple tv",
        "subscription", "subscription plan",
        "monthly plan", "annual plan", "yearly plan",
        "saas", "software subscription", "tool subscription",
        "linkedin premium", "github pro", "notion pro",
    ],

    # ── Travel ───────────────────────────────────────────────────────────
    "Travel": [
        "flight ticket", "air ticket", "airfare",
        "indigo", "air india", "spicejet", "vistara",
        "irctc", "train booking", "tatkal ticket",
        "hotel booking", "hotel stay", "oyo", "makemytrip",
        "goibibo", "yatra", "cleartrip",
        "hostel booking", "airbnb",
        "tour package", "vacation package", "holiday package",
        "travel insurance", "visa fee",
        "forex", "foreign exchange", "travel money",
    ],

    # ── Personal ─────────────────────────────────────────────────────────
    "Personal": [
        "salon", "hair salon", "haircut", "hair spa",
        "spa", "massage", "body massage",
        "gym", "gym membership", "fitness center",
        "yoga class", "fitness membership",
        "cosmetics", "makeup", "foundation", "lipstick",
        "skincare", "moisturizer", "sunscreen",
        "personal care", "deodorant", "perfume",
        "laundry", "dry cleaning", "ironing",
    ],

    # ── Family ───────────────────────────────────────────────────────────
    "Family": [
        "gift", "gift purchase", "birthday gift", "anniversary gift",
        "family expense", "family outing",
        "toys", "kids toys", "board game",
        "kids items", "baby products", "diapers",
        "school supplies", "school bag", "stationery",
        "pet food", "dog food", "cat food",
        "vet", "vet bill", "pet care", "pet grooming",
        "wedding gift", "wedding shopping",
    ],

    # ── Finance ──────────────────────────────────────────────────────────
    "Finance": [
        "emi", "emi payment", "loan emi", "home loan emi",
        "personal loan", "loan payment", "loan repayment",
        "credit card bill", "credit card payment", "card dues",
        "mutual fund", "mf investment", "sip", "sip payment",
        "stock purchase", "share purchase", "trading",
        "demat account", "brokerage",
        "income tax", "advance tax", "tds",
        "gst payment", "professional tax",
        "fd deposit", "rd deposit", "ppf deposit",
        "nps contribution", "investment",
    ],

    # ── Insurance ────────────────────────────────────────────────────────
    "Insurance": [
        "insurance premium", "policy premium", "policy payment",
        "life insurance", "term insurance", "term plan",
        "health insurance", "mediclaim", "medical insurance",
        "vehicle insurance", "car insurance", "bike insurance",
        "home insurance", "travel insurance",
        "lic premium", "lic payment", "hdfc life",
        "max life", "icici prudential", "bajaj allianz",
    ],

    # ── Misc ─────────────────────────────────────────────────────────────
    "Misc": [
        "misc", "miscellaneous", "other expense",
        "unknown", "general expense",
        "upi transfer", "bank transfer", "neft transfer",
        "imps transfer", "rtgs transfer",
        "payment", "online payment", "amount paid",
        "debit", "credit entry", "transaction",
        "money sent", "money received",
        "cash withdrawal", "atm withdrawal",
    ],
}

# =====================================================
# Transaction templates
# =====================================================
templates = [
    "{}",
    "{} payment",
    "{} bill",
    "{} expense",
    "paid for {}",
    "paid {}",
    "bought {}",
    "{} purchase",
    "{} order",
    "{} transaction",
    "{} charge",
    "{} fee",
    "monthly {}",
    "weekly {}",
    "annual {}",
    "{} via upi",
    "{} via gpay",
    "{} via phonepe",
    "{} via paytm",
    "upi payment for {}",
    "online {}",
    "service for {}",
    "{} maintenance",
    "{} repair",
    "{} booking",
    "{} subscription",
    "{} recharge",
    "{} credited",
    "{} received",
    "{} transferred",
]

# =====================================================
# Generate balanced dataset — 500 samples per category
# =====================================================
SAMPLES_PER_CATEGORY = 500
data = []

for category in FINAL_CATEGORIES:
    keywords = category_keywords.get(category, ["expense"])
    count = 0

    while count < SAMPLES_PER_CATEGORY:
        word = random.choice(keywords)
        template = random.choice(templates)
        text = template.format(word)
        data.append({"description": text, "category": category})
        count += 1

# =====================================================
# Extra income-specific samples (high-signal phrases)
# =====================================================
income_phrases = [
    "salary for march", "salary march 2024", "salary april",
    "salary credited to account", "monthly salary received",
    "bonus for q4", "annual bonus credited", "performance bonus",
    "freelance project payment", "freelance work payment",
    "refund from amazon", "refund from flipkart", "order refund",
    "cashback from hdfc", "cashback credited", "credit card cashback",
    "interest credited by bank", "savings account interest",
    "fd maturity amount", "dividend from zerodha",
    "stipend for june", "internship stipend credited",
    "incentive payment", "commission received", "consulting fees",
    "rental income received", "rent from tenant",
    "reimbursement from company", "travel reimbursement",
]
for phrase in income_phrases:
    data.append({"description": phrase, "category": "Income"})

# =====================================================
# Generic noise → Misc
# =====================================================
noise_texts = [
    "payment", "upi transfer", "bank debit", "transaction",
    "online payment", "amount paid", "debit", "credit",
    "transfer", "money sent", "neft", "imps",
    "cash deposit", "atm withdrawal",
]
for text in noise_texts:
    data.append({"description": text, "category": "Misc"})

# =====================================================
# Shuffle and train
# =====================================================
random.shuffle(data)
print(f"[train_category] Total training samples: {len(data)}")

# Breakdown per category
from collections import Counter
counts = Counter(d["category"] for d in data)
for cat in FINAL_CATEGORIES:
    print(f"  {cat:<16} {counts.get(cat, 0)} samples")

train_category_model(data)
print("[train_category] Category model trained successfully.")