# routes/loan_application.py

"""
Borrower Loan Application Routes (Phase 1).

Scope for this phase, intentionally minimal:
    GET  /loan/apply              -> borrower-facing application form
    POST /api/loan-applications   -> creates a new PENDING application

Explicitly NOT built here (later phases): lender review queue, credit-risk
assessment triggering, approve/reject, borrower status page.

DATA OWNERSHIP:
Every application explicitly stores borrower_id (the authenticated
consumer submitting it) and lender_id (a validated, active lender the
consumer selected). Lender-side affordability/financial-behavior lookups
in later phases should resolve the borrower from application.borrower_id
— never from the lender's own session.
"""

import json
from functools import wraps

from flask import Blueprint, request, jsonify, session, render_template, abort

from utils.decorators import login_required
from utils.db import get_db

loan_application_bp = Blueprint("loan_application", __name__)

# Exact 20-field production credit-model schema. This must match the
# schema used by the lender-side workspace and the underlying model —
# do not add, remove, or rename fields here.
APPLICATION_FIELDS = [
    "checking_account", "duration_months", "credit_history", "purpose",
    "credit_amount", "savings_account", "employment_since", "installment_rate",
    "personal_status_sex", "other_debtors", "residence_since", "property",
    "age", "other_installment_plans", "housing", "existing_credits",
    "job", "dependents", "telephone", "foreign_worker",
]

NUMERIC_FIELDS = {
    "duration_months", "credit_amount", "installment_rate", "existing_credits",
    "residence_since", "age", "dependents",
}


def consumer_required(view_func):
    """
    Restricts a route to authenticated users with role='consumer' and
    status='active'. Must be applied together with @login_required.

    This is the borrower-only creation boundary: a lender or admin is
    authenticated, but must not be able to create a borrower application
    through this endpoint.
    """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            abort(401)

        conn = get_db()
        try:
            user = conn.execute(
                "SELECT id, role, status FROM users WHERE id=%s", (user_id,)
            ).fetchone()
        finally:
            conn.close()

        if not user or user["role"] != "consumer" or user["status"] != "active":
            abort(403)

        return view_func(*args, **kwargs)
    return wrapper


@loan_application_bp.route("/loan/apply", methods=["GET"])
@login_required
@consumer_required
def loan_apply_page():
    """
    Renders the borrower-facing loan application form.

    The applicant's name/email come from the authenticated account —
    the borrower is never asked to type their own name. Only active
    lenders (role='lender', status='active') are offered as choices.
    """
    user_id = session.get("user_id")
    conn = get_db()
    try:
        borrower = conn.execute(
            "SELECT id, name, email FROM users WHERE id=%s", (user_id,)
        ).fetchone()
        lenders = conn.execute(
            "SELECT id, name FROM users WHERE role='lender' AND status='active' ORDER BY name ASC"
        ).fetchall()
    finally:
        conn.close()

    return render_template("loan_application.html", borrower=borrower, lenders=lenders)


def _validate_application_payload(applicant):
    errors = []
    if not isinstance(applicant, dict):
        return ["Application data must be an object."]

    for field in APPLICATION_FIELDS:
        value = applicant.get(field)
        if value is None or value == "":
            errors.append(f"Missing required field: {field}")
            continue
        if field in NUMERIC_FIELDS:
            try:
                float(value)
            except (TypeError, ValueError):
                errors.append(f"Field '{field}' must be numeric.")

    return errors


@loan_application_bp.route("/api/lenders", methods=["GET"])
@login_required
def list_lenders():
    """
    Minimal borrower-facing lender list for clients that can't consume
    the server-rendered <select> in loan_application.html (e.g. the
    Flutter app).

    Reuses the exact same eligibility query as loan_apply_page() above
    — role='lender' AND status='active', ordered by name — so this
    never drifts from what the web borrower form already offers. Only
    id/name are returned; no password, email, role, or status fields.
    Any authenticated user may call this (it does not accept or use a
    borrower_id), matching this endpoint's existing @login_required
    convention.
    """
    conn = get_db()
    try:
        lenders = conn.execute(
            "SELECT id, name FROM users WHERE role='lender' AND status='active' ORDER BY name ASC"
        ).fetchall()
    except Exception:
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Failed to load lenders. Please try again."],
        }), 500
    finally:
        conn.close()

    return jsonify({
        "status": "success",
        "lenders": [{"id": row["id"], "name": row["name"]} for row in lenders],
    })


@loan_application_bp.route("/api/loan-applications", methods=["POST"])
@login_required
@consumer_required
def create_loan_application():
    """
    Creates a new PENDING loan application tied to the authenticated
    borrower and a server-validated, active lender.
    """
    payload = request.get_json(silent=True)
    if payload is None or not isinstance(payload, dict):
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Request body must be a JSON object."],
        }), 400

    lender_id = payload.get("lender_id")
    applicant = payload.get("applicant")

    errors = []
    if not lender_id:
        errors.append("A lender must be selected.")
    errors.extend(_validate_application_payload(applicant))

    if errors:
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": errors,
        }), 400

    borrower_id = session.get("user_id")

    conn = get_db()
    try:
        # Never trust the client-selected lender: re-verify server-side
        # that it exists, is role='lender', and is status='active'.
        lender = conn.execute(
            "SELECT id FROM users WHERE id=%s AND role='lender' AND status='active'",
            (lender_id,),
        ).fetchone()
        if not lender:
            return jsonify({
                "status": "error",
                "error_type": "validation_error",
                "errors": ["Selected lender is not available."],
            }), 400

        application_data = {field: applicant[field] for field in APPLICATION_FIELDS}

        row = conn.execute(
            """
            INSERT INTO loan_applications (borrower_id, lender_id, application_data, status)
            VALUES (%s, %s, %s, 'PENDING')
            RETURNING id, status, created_at
            """,
            (borrower_id, lender_id, json.dumps(application_data)),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Failed to submit application. Please try again."],
        }), 500
    finally:
        conn.close()

    return jsonify({
        "status": "success",
        "application_id": row["id"],
        "application_status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }), 201


@loan_application_bp.route("/api/loan-applications", methods=["GET"])
@login_required
@consumer_required
def list_loan_applications():
    """
    Returns the authenticated borrower's own loan applications.

    loan_applications.status is the single source of truth written by the
    lender workspace (PENDING/APPROVED/REJECTED/WITHDRAWN) — this endpoint
    only reads that same column, it never maintains a separate status
    store. Scoped strictly to borrower_id = the authenticated session
    user; borrower_id is never taken from the client, so one borrower can
    never see another borrower's applications.
    """
    borrower_id = session.get("user_id")

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT la.id, la.status, la.created_at, la.application_data,
                   u.name AS lender_name
            FROM loan_applications la
            JOIN users u ON u.id = la.lender_id
            WHERE la.borrower_id = %s
            ORDER BY la.created_at DESC
            """,
            (borrower_id,),
        ).fetchall()
    finally:
        conn.close()

    applications = []
    for row in rows:
        data = row["application_data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                data = {}
        elif not isinstance(data, dict):
            data = {}

        applications.append({
            "application_id": row["id"],
            "lender_name": row["lender_name"],
            "purpose": data.get("purpose"),
            "loan_amount": data.get("credit_amount"),
            "submitted_at": row["created_at"].isoformat() if row["created_at"] else None,
            "status": row["status"],
        })

    return jsonify({"status": "success", "applications": applications})


# ---------------------------------------------------------------------
# BORROWER WITHDRAWAL (Phase 5)
#
# The only new transition this endpoint allows is PENDING -> WITHDRAWN.
# It never accepts borrower_id from the client — ownership is exclusively
# session["user_id"], identical to every other route in this file. The
# UPDATE is atomic and conditional on status='PENDING', so an
# application that has just been APPROVED/REJECTED by the lender (or
# already withdrawn) can never be silently overwritten by a
# concurrent/late withdrawal request.
# ---------------------------------------------------------------------
@loan_application_bp.route("/api/loan-applications/<int:application_id>/withdraw", methods=["POST"])
@login_required
@consumer_required
def withdraw_loan_application(application_id):
    """
    Withdraws the authenticated borrower's own PENDING application.

    Ownership: `id = %s AND borrower_id = session["user_id"]` is the
    entire authorization boundary — an application that doesn't exist
    and one that belongs to another borrower are both reported as 404,
    so this endpoint never confirms or denies another borrower's
    application ids.

    Only loan_applications.status and updated_at are written. No new
    status value, flag, or store is introduced — WITHDRAWN is already
    part of the existing status CHECK constraint.
    """
    borrower_id = session.get("user_id")

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, status FROM loan_applications WHERE id=%s AND borrower_id=%s",
            (application_id, borrower_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    if row["status"] != "PENDING":
        return jsonify({
            "status": "error",
            "error_type": "conflict",
            "errors": ["Only a pending application can be withdrawn."],
        }), 409

    conn = get_db()
    try:
        updated = conn.execute(
            """
            UPDATE loan_applications
            SET status = 'WITHDRAWN', updated_at = now()
            WHERE id = %s AND borrower_id = %s AND status = 'PENDING'
            RETURNING id, status, updated_at
            """,
            (application_id, borrower_id),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Failed to withdraw application. Please try again."],
        }), 500
    finally:
        conn.close()

    if not updated:
        # Finalized (or withdrawn) by a concurrent request between the
        # check above and this UPDATE — report the same conflict rather
        # than a fabricated success.
        return jsonify({
            "status": "error",
            "error_type": "conflict",
            "errors": ["This application can no longer be withdrawn."],
        }), 409

    return jsonify({
        "status": "success",
        "application_id": updated["id"],
        "application_status": updated["status"],
        "updated_at": updated["updated_at"].isoformat() if updated["updated_at"] else None,
    })