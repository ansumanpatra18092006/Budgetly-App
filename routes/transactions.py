from flask import Blueprint, request, jsonify, session, make_response
from utils.decorators import login_required
from utils.db import get_db
from datetime import datetime
import csv
import io

# Import your ML category predictor
from ml.category_model import predict_category

transactions_bp = Blueprint("transactions", __name__)


# ===============================
# Add Transaction (with Auto Detect)
# ===============================
@transactions_bp.route("/add-transaction", methods=["POST"])
@login_required
def add_transaction():
    data = request.get_json()
    user_id = session["user_id"]

    description = data.get("description", "")
    amount = float(data.get("amount", 0))
    t_type = data.get("type", "expense")
    category = data.get("category")
    date = data.get("date", datetime.today().strftime("%Y-%m-%d"))

    # ===== Allowed manual categories =====
    ALLOWED_CATEGORIES = {
        "Food", "Transport", "Rent", "Utilities", "Shopping",
        "Health", "Education", "Entertainment", "Subscriptions",
        "Travel", "Personal", "Family", "Finance", "Insurance", "Misc"
    }

    # -------- Auto Detect --------
    if not category:  # Auto-detect selected
        try:
            category = predict_category(description)
        except Exception:
            category = None

    # -------- Normalize category --------
    if category not in ALLOWED_CATEGORIES:
        category = "Misc"

    # -------- Save to DB --------
    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO transactions (user_id, description, amount, type, category, date)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        description,
        amount,
        t_type,
        category,
        date
    ))
    conn.commit()
    tid = cursor.lastrowid
    conn.close()

    return jsonify({"success": True, "id": tid})

# ===============================
# Get Transactions (Filters)
# ===============================
@transactions_bp.route("/get-transactions")
@login_required
def get_transactions():
    user_id = session["user_id"]

    start = request.args.get("start")
    end = request.args.get("end")
    category = request.args.get("category")
    t_type = request.args.get("type")
    search = request.args.get("search")

    query = "SELECT * FROM transactions WHERE user_id=?"
    params = [user_id]

    if start:
        query += " AND date >= ?"
        params.append(start)

    if end:
        query += " AND date <= ?"
        params.append(end)

    if category and category != "All":
        query += " AND category=?"
        params.append(category)

    if t_type and t_type != "All":
        query += " AND type=?"
        params.append(t_type)

    if search and search.strip():
        query += " AND LOWER(description) LIKE ?"
        params.append(f"%{search.lower()}%")

    query += " ORDER BY date DESC, id DESC"

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()

    return jsonify({"transactions": [dict(r) for r in rows]})


# ===============================
# Delete Transaction
# ===============================
@transactions_bp.route("/delete-transaction/<int:tid>", methods=["DELETE"])
@login_required
def delete_transaction(tid):
    conn = get_db()
    conn.execute(
        "DELETE FROM transactions WHERE id=? AND user_id=?",
        (tid, session["user_id"])
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True})


# ===============================
# Update Transaction
# ===============================
@transactions_bp.route("/update-transaction/<int:tid>", methods=["PUT"])
@login_required
def update_transaction(tid):
    data = request.get_json()

    conn = get_db()
    conn.execute("""
        UPDATE transactions
        SET description=?, amount=?, category=?, type=?, date=?
        WHERE id=? AND user_id=?
    """, (
        data["description"],
        float(data["amount"]),
        data["category"],
        data["type"],
        data["date"],
        tid,
        session["user_id"]
    ))
    conn.commit()
    conn.close()

    return jsonify({"success": True})


# ===============================
# Clear All Transactions
# ===============================
@transactions_bp.route("/clear-all-transactions", methods=["POST"])
@login_required
def clear_all_transactions():
    conn = get_db()
    conn.execute(
        "DELETE FROM transactions WHERE user_id=?",
        (session["user_id"],)
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True})


# ===============================
# Export CSV
# ===============================
@transactions_bp.route("/export-transactions")
@login_required
def export_transactions():
    user_id = session["user_id"]

    conn = get_db()
    rows = conn.execute(
        "SELECT description, amount, type, category, date FROM transactions WHERE user_id=?",
        (user_id,)
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["description", "amount", "type", "category", "date"])

    for r in rows:
        writer.writerow([
            r["description"],
            r["amount"],
            r["type"],
            r["category"],
            r["date"]
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=transactions.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

# ===============================
# Import CSV
# ===============================
@transactions_bp.route("/import-transactions", methods=["POST"])
@login_required
def import_transactions():
    user_id = session["user_id"]

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files["file"]

    if file.filename == "":
        return jsonify({"success": False, "error": "Empty filename"}), 400

    try:
        stream = io.StringIO(file.stream.read().decode("UTF-8"))
        reader = csv.DictReader(stream)

        conn = get_db()
        inserted = 0

        ALLOWED_CATEGORIES = {
            "Food", "Transport", "Rent", "Utilities", "Shopping",
            "Health", "Education", "Entertainment", "Subscriptions",
            "Travel", "Personal", "Family", "Finance", "Insurance", "Misc"
        }

        for row in reader:
            description = row.get("description", "").strip()
            amount = float(row.get("amount", 0))
            t_type = row.get("type", "expense").strip().lower()
            category = row.get("category", "")
            date = row.get("date", datetime.today().strftime("%Y-%m-%d"))

            # Auto-detect category if empty
            if not category:
                try:
                    category = predict_category(description)
                except:
                    category = "Misc"

            if category not in ALLOWED_CATEGORIES:
                category = "Misc"

            conn.execute("""
                INSERT INTO transactions (user_id, description, amount, type, category, date)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                description,
                amount,
                t_type,
                category,
                date
            ))

            inserted += 1

        conn.commit()
        conn.close()

        return jsonify({"success": True, "imported": inserted})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500