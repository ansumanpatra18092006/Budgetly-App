import random
import re
from collections import Counter
from ml.category_model import train_category_model

random.seed(42)

# =====================================================
# FINAL CATEGORIES (STRICT - match frontend)
# =====================================================
FINAL_CATEGORIES = [
    "Food",
    "Transport",
    "Shopping",
    "Health",
    "Education",
    "Entertainment",
    "Housing",
    "Finance",
    "Misc",
]

# =====================================================
# CATEGORY KEYWORDS (MERGED + SIMPLIFIED)
# =====================================================
category_keywords = {

    "Food": [
        "swiggy", "zomato", "restaurant", "cafe", "dhaba",
        "grocery", "milk", "vegetables", "fruits", "bakery",
        "pizza", "burger", "food", "meal"
    ],

    "Transport": [
        "uber", "ola", "auto", "taxi", "bus", "metro",
        "train", "petrol", "diesel", "fuel", "toll",
        "parking", "car service", "bike repair"
    ],

    "Shopping": [
        "amazon", "flipkart", "myntra", "store", "shop",
        "clothes", "shoes", "electronics", "mobile",
        "laptop", "purchase", "order"
    ],

    "Health": [
        "hospital", "clinic", "doctor", "medicine",
        "pharmacy", "test", "lab", "checkup"
    ],

    "Education": [
        "school", "college", "tuition", "course",
        "books", "exam", "fees", "study"
    ],

    "Entertainment": [
        "netflix", "spotify", "movie", "cinema",
        "game", "subscription", "youtube",
        "concert", "show"
    ],

    "Housing": [
        "rent", "electricity", "water bill",
        "wifi", "broadband", "gas", "maintenance"
    ],

    "Finance": [
        "salary", "loan", "emi", "credit card",
        "investment", "insurance", "tax",
        "interest", "refund", "cashback"
    ],

    "Misc": [
        "payment", "transaction", "transfer",
        "upi", "debit", "credit"
    ]
}

# =====================================================
# NORMALIZATION (VERY IMPORTANT)
# =====================================================
def normalize(text):
    text = re.sub(r'[^a-zA-Z0-9 ]', ' ', text)
    return re.sub(r'\s+', ' ', text).lower().strip()

# =====================================================
# TEMPLATES (LESS NOISE, MORE REALISTIC)
# =====================================================
templates = [
    "{}",
    "{} payment",
    "paid {}",
    "{} bill",
    "{} expense",
    "upi payment for {}",
    "{} via upi",
]

# =====================================================
# GENERATE DATA
# =====================================================
SAMPLES_PER_CATEGORY = 300
data = []

for category in FINAL_CATEGORIES:
    keywords = category_keywords[category]

    for _ in range(SAMPLES_PER_CATEGORY):
        word = random.choice(keywords)
        template = random.choice(templates)
        text = template.format(word)

        data.append({
            "description": normalize(text),
            "category": category
        })

# =====================================================
# ADD REAL-WORLD MERCHANT DATA (CRITICAL)
# =====================================================
real_samples = [
    ("netflix entertainment services india llp", "Entertainment"),
    ("spotify india subscription", "Entertainment"),
    ("mahadev grocery", "Food"),
    ("radhamadhab filling station", "Transport"),
    ("madhavi xerox", "Shopping"),
    ("pakwaan restaurant", "Food"),
    ("it zone electronics", "Shopping"),
    ("electricity bill payment", "Housing"),
    ("wifi broadband bill", "Housing"),
    ("salary credited", "Finance"),
    ("loan emi payment", "Finance"),
]

for text, category in real_samples:
    text = normalize(text)

    # add variations
    variations = [
        text,
        f"{text} payment",
        f"paid to {text}",
        f"{text} via upi"
    ]

    for v in variations:
        data.append({
            "description": normalize(v),
            "category": category
        })

# =====================================================
# REDUCE NOISE (IMPORTANT)
# =====================================================
noise_texts = [
    "payment", "upi transfer", "bank debit", "transaction"
]

for text in noise_texts:
    data.append({
        "description": normalize(text),
        "category": "Misc"
    })

# =====================================================
# SHUFFLE + TRAIN
# =====================================================
random.shuffle(data)

print(f"[train_category] Total samples: {len(data)}")

counts = Counter(d["category"] for d in data)
for cat in FINAL_CATEGORIES:
    print(f"{cat:<15}: {counts.get(cat, 0)}")

# =====================================================
# TRAIN MODEL
# =====================================================
train_category_model(data)

print("[train_category] Model trained successfully")