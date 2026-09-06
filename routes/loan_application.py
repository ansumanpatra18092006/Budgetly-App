# routes/loan_application.py

"""
Borrower loan application and post-approval lifecycle routes.

The application status (PENDING/APPROVED/REJECTED/WITHDRAWN) remains the
workflow source of truth. Once approved, loan_state tracks the demo
sanction/disbursement/repayment lifecycle.

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
    """Return the authenticated borrower's applications plus loan lifecycle summary."""
    borrower_id = session.get("user_id")
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT la.id, la.status, la.created_at, la.application_data,
                   la.approved_amount, la.interest_rate, la.emi_amount,
                   la.loan_term_months, la.loan_state, la.disbursed_at,
                   la.outstanding_amount,
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
            "loan": {
                "state": row["loan_state"] or "APPLICATION",
                "approved_amount": row["approved_amount"],
                "interest_rate": row["interest_rate"],
                "emi_amount": row["emi_amount"],
                "tenure_months": row["loan_term_months"],
                "disbursed_at": row["disbursed_at"].isoformat() if row["disbursed_at"] else None,
                "outstanding_amount": row["outstanding_amount"],
            },
        })

    return jsonify({"status": "success", "applications": applications})


@loan_application_bp.route("/api/loan-applications/<int:application_id>", methods=["GET"])
@login_required
@consumer_required
def get_loan_application(application_id):
    """Return borrower-owned application detail and repayment history."""
    borrower_id = session.get("user_id")
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT la.id, la.status, la.created_at, la.updated_at, la.application_data,
                   la.approved_amount, la.interest_rate, la.emi_amount,
                   la.loan_term_months, la.loan_state, la.disbursed_at,
                   la.outstanding_amount, la.decided_at,
                   u.name AS lender_name
            FROM loan_applications la
            JOIN users u ON u.id = la.lender_id
            WHERE la.id = %s AND la.borrower_id = %s
            """,
            (application_id, borrower_id),
        ).fetchone()
        if not row:
            return None
        repayments = conn.execute(
            """
            SELECT id, amount, remaining_amount, paid_at
            FROM loan_repayments
            WHERE application_id = %s AND borrower_id = %s
            ORDER BY paid_at DESC
            """,
            (application_id, borrower_id),
        ).fetchall()
    finally:
        conn.close()

    if not row:
        abort(404)

    data = row["application_data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = {}
    elif not isinstance(data, dict):
        data = {}

    total_repaid = sum(float(r["amount"] or 0) for r in repayments)
    application = {
        "application_id": row["id"],
        "lender_name": row["lender_name"],
        "purpose": data.get("purpose"),
        "loan_amount": data.get("credit_amount"),
        "tenure_months": data.get("duration_months"),
        "submitted_at": row["created_at"].isoformat() if row["created_at"] else None,
        "status": row["status"],
        "applicant": data,
        "decision": None,
        "loan": {
            "state": row["loan_state"] or "APPLICATION",
            "approved_amount": row["approved_amount"],
            "interest_rate": row["interest_rate"],
            "emi_amount": row["emi_amount"],
            "tenure_months": row["loan_term_months"],
            "disbursed_at": row["disbursed_at"].isoformat() if row["disbursed_at"] else None,
            "outstanding_amount": row["outstanding_amount"],
            "total_repaid": total_repaid,
            "repayments": [
                {"id": r["id"], "amount": r["amount"], "remaining_amount": r["remaining_amount"],
                 "paid_at": r["paid_at"].isoformat() if r["paid_at"] else None}
                for r in repayments
            ],
        },
    }
    if row["status"] in ("APPROVED", "REJECTED"):
        application["decision"] = {
            "status": row["status"],
            "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
            "message": None,
        }

    return jsonify({"status": "success", "application": application})


@loan_application_bp.route("/api/loan-applications/<int:application_id>/activate", methods=["POST"])
@login_required
@consumer_required
def activate_loan_application(application_id):
    """Perform the expo's explicit demo disbursement: SANCTIONED -> ACTIVE."""
    borrower_id = session.get("user_id")
    conn = get_db()
    try:
        updated = conn.execute(
            """
            UPDATE loan_applications
            SET loan_state='ACTIVE', disbursed_at=now(),
                approved_amount=COALESCE(approved_amount, NULLIF((application_data->>'credit_amount'), '')::double precision),
                interest_rate=COALESCE(interest_rate, 12.0),
                loan_term_months=COALESCE(loan_term_months, NULLIF((application_data->>'duration_months'), '')::integer),
                emi_amount=COALESCE(
                    emi_amount,
                    CASE
                        WHEN COALESCE(loan_term_months, NULLIF((application_data->>'duration_months'), '')::integer) IS NULL
                          OR COALESCE(loan_term_months, NULLIF((application_data->>'duration_months'), '')::integer) < 1
                          OR COALESCE(approved_amount, NULLIF((application_data->>'credit_amount'), '')::double precision) IS NULL
                        THEN NULL
                        WHEN COALESCE(interest_rate, 12.0) = 0
                        THEN COALESCE(approved_amount, NULLIF((application_data->>'credit_amount'), '')::double precision) / COALESCE(loan_term_months, NULLIF((application_data->>'duration_months'), '')::integer)
                        ELSE COALESCE(approved_amount, NULLIF((application_data->>'credit_amount'), '')::double precision)
                             * (COALESCE(interest_rate, 12.0) / 12.0 / 100.0)
                             * POWER(1 + (COALESCE(interest_rate, 12.0) / 12.0 / 100.0), COALESCE(loan_term_months, NULLIF((application_data->>'duration_months'), '')::integer))
                             / (POWER(1 + (COALESCE(interest_rate, 12.0) / 12.0 / 100.0), COALESCE(loan_term_months, NULLIF((application_data->>'duration_months'), '')::integer)) - 1)
                    END
                ),
                disbursed_at=now(),
                outstanding_amount=COALESCE(approved_amount, NULLIF((application_data->>'credit_amount'), '')::double precision),
                updated_at=now()
            WHERE id=%s AND borrower_id=%s AND status='APPROVED' AND COALESCE(loan_state, 'SANCTIONED') IN ('SANCTIONED', 'APPLICATION')
            RETURNING id, loan_state, approved_amount, interest_rate, emi_amount,
                      loan_term_months, disbursed_at, outstanding_amount
            """,
            (application_id, borrower_id),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({"status":"error","error_type":"internal_error","errors":["Failed to activate the loan."]}), 500
    finally:
        conn.close()

    if not updated:
        return jsonify({"status":"error","error_type":"conflict",
                        "errors":["Only an approved loan can be activated."]}), 409
    return jsonify({
        "status":"success", "application_id":updated["id"],
        "loan": {
            "state": updated["loan_state"], "approved_amount": updated["approved_amount"],
            "interest_rate": updated["interest_rate"], "emi_amount": updated["emi_amount"],
            "tenure_months": updated["loan_term_months"],
            "disbursed_at": updated["disbursed_at"].isoformat() if updated["disbursed_at"] else None,
            "outstanding_amount": updated["outstanding_amount"],
        },
    })


@loan_application_bp.route("/api/loan-applications/<int:application_id>/repay", methods=["POST"])
@login_required
@consumer_required
def repay_loan_application(application_id):
    """Record a borrower repayment against an ACTIVE demo loan."""
    borrower_id = session.get("user_id")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError):
        amount = 0.0
    if amount <= 0:
        return jsonify({"status":"error","error_type":"validation_error","errors":["Repayment amount must be greater than zero."]}), 400

    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, approved_amount, outstanding_amount, loan_state
               FROM loan_applications
               WHERE id=%s AND borrower_id=%s AND status='APPROVED' AND loan_state='ACTIVE'
               FOR UPDATE""",
            (application_id, borrower_id),
        ).fetchone()
        if not row:
            conn.rollback()
            return jsonify({"status":"error","error_type":"conflict","errors":["Only an active approved loan can accept repayments."]}), 409

        outstanding = float(row["outstanding_amount"] if row["outstanding_amount"] is not None else row["approved_amount"] or 0)
        if amount > outstanding + 1e-9:
            conn.rollback()
            return jsonify({"status":"error","error_type":"validation_error","errors":[f"Repayment cannot exceed the outstanding balance ({outstanding:.2f})."]}), 400

        remaining = max(0.0, outstanding - amount)
        repayment = conn.execute(
            """INSERT INTO loan_repayments (application_id, borrower_id, amount, remaining_amount)
               VALUES (%s,%s,%s,%s)
               RETURNING id, amount, remaining_amount, paid_at""",
            (application_id, borrower_id, amount, remaining),
        ).fetchone()
        new_state = 'CLOSED' if remaining <= 1e-9 else 'ACTIVE'
        updated = conn.execute(
            """UPDATE loan_applications
               SET outstanding_amount=%s, loan_state=%s, updated_at=now()
               WHERE id=%s AND borrower_id=%s AND loan_state='ACTIVE'
               RETURNING loan_state, outstanding_amount""",
            (remaining, new_state, application_id, borrower_id),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({"status":"error","error_type":"internal_error","errors":["Failed to record repayment. Please try again."]}), 500
    finally:
        conn.close()

    return jsonify({
        "status":"success", "application_id":application_id,
        "loan_state":updated["loan_state"], "outstanding_amount":updated["outstanding_amount"],
        "repayment": {"id":repayment["id"], "amount":repayment["amount"],
                      "remaining_amount":repayment["remaining_amount"],
                      "paid_at":repayment["paid_at"].isoformat() if repayment["paid_at"] else None},
    })


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