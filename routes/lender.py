# routes/lender.py

"""
Page + API routes for the lender/analyst workspace.

PHASE 0: page routes only (lender_login_page, lender_workspace_page).
PHASE 2: the lender-side application queue (read-only).
PHASE 3 (this update): running the existing credit-risk assessment
against a queued application's own stored data.

This module does not implement Credit Assessment itself — no model,
preprocessing, feature encoding, threshold, decision-policy, SHAP,
anomaly, scenario, or fairness code lives here. It calls the existing
services.credit_risk_service.assess_credit_risk() wrapper, which is
the same verified path used by POST /api/credit-risk/assess in
credit_risk.py (unchanged, untouched).

OWNERSHIP / AUTHORIZATION:
The lender identity for every query below comes exclusively from
session["user_id"] (set at login, re-validated as an active lender on
every request by @lender_required). It is never accepted from the
frontend. Every query filters `lender_id = session["user_id"]` at the
SQL level, so a request for another lender's application simply
matches zero rows and falls through to the same 404 used for a
nonexistent id — no separate "forbidden, but it does exist" branch
that would leak which application ids are in use.

BORROWER IDENTITY:
The applicant shown in the queue/detail views is resolved from
loan_applications.borrower_id, joined against users — never from the
lender's own session["user_id"]. The signed-in lender is always the
reviewer, never the applicant.

Registration (add to app.py, alongside the other blueprints):

    from routes.lender import lender_bp
    app.register_blueprint(lender_bp)
"""

import json

from flask import Blueprint, render_template, session, jsonify, abort, request

from utils.decorators import lender_required
from utils.db import get_db
from services.credit_risk_service import (
    assess_credit_risk,
    explain_credit_risk,
    analyze_credit_scenario,
    check_credit_anomaly,
)
from services.financial_behavior_service import get_financial_behavior_profile
from services.affordability_service import calculate_affordability

# Scenario Analysis (PHASE 7) may only vary these fields for a stored
# application. Anything else in the request body is ignored — the
# client can never use this endpoint to replace the baseline applicant
# payload itself, only nudge these four values away from what's
# actually stored in loan_applications.application_data.
SCENARIO_ALLOWED_FIELDS = {
    "duration_months",
    "credit_amount",
    "installment_rate",
    "existing_credits",
}

lender_bp = Blueprint("lender", __name__)


# ---------------------------------------------------------------------
# Code -> human-readable label maps for the 20-field application schema
# (public.loan_applications.application_data). These mirror the exact
# option labels already used in the borrower/lender assessment forms
# (loan_application.html / lender_workspace.html) so the queue and the
# manual assessment form never disagree on what a code means.
# ---------------------------------------------------------------------

PURPOSE_LABELS = {
    "A40": "Car (new)", "A41": "Car (used)", "A42": "Furniture / equipment",
    "A43": "Radio / television", "A44": "Domestic appliances", "A45": "Repairs",
    "A46": "Education", "A48": "Retraining", "A49": "Business", "A410": "Other",
}
CHECKING_ACCOUNT_LABELS = {
    "A11": "Overdrawn", "A12": "Low balance", "A13": "Adequate balance",
    "A14": "No checking account",
}
CREDIT_HISTORY_LABELS = {
    "A30": "No credit taken previously", "A31": "All previous credit paid on time",
    "A32": "Existing credit paid on time so far", "A33": "Repayment delays in the past",
    "A34": "Critical credit history",
}
OTHER_INSTALLMENT_LABELS = {"A141": "Bank", "A142": "Retail / store financing", "A143": "None"}
SAVINGS_LABELS = {
    "A61": "Minimal savings", "A62": "Low savings balance", "A63": "Moderate savings balance",
    "A64": "High savings balance", "A65": "No savings account / unknown",
}
EMPLOYMENT_LABELS = {
    "A71": "Unemployed", "A72": "Less than 1 year", "A73": "1 – 4 years",
    "A74": "4 – 7 years", "A75": "7+ years",
}
JOB_LABELS = {
    "A171": "Unemployed / unskilled, non-resident", "A172": "Unskilled, resident",
    "A173": "Skilled employee / official", "A174": "Management / self-employed / highly qualified",
}
HOUSING_LABELS = {"A151": "Rent", "A152": "Own", "A153": "For free"}
PROPERTY_LABELS = {
    "A121": "Real estate", "A122": "Building society savings / life insurance",
    "A123": "Car or other", "A124": "Unknown / no property",
}
PERSONAL_STATUS_LABELS = {
    "A91": "Male : divorced / separated", "A92": "Female : divorced / separated / married",
    "A93": "Male : single", "A94": "Male : married / widowed", "A95": "Female : single",
}
TELEPHONE_LABELS = {"A191": "None", "A192": "Yes, under applicant's name"}
FOREIGN_WORKER_LABELS = {"A201": "Yes", "A202": "No"}
OTHER_DEBTORS_LABELS = {"A101": "None", "A102": "Co-applicant", "A103": "Guarantor"}
INSTALLMENT_RATE_LABELS = {
    "1": "Low (~1 band)", "2": "Moderate (~2 band)", "3": "High (~3 band)", "4": "Very high (~4 band)",
}

# (application_data key, display label, optional code->label map)
# Covers all 20 fields in loan_application.py::APPLICATION_FIELDS.
FIELD_DISPLAY = [
    ("checking_account", "Checking Account", CHECKING_ACCOUNT_LABELS),
    ("credit_history", "Credit History", CREDIT_HISTORY_LABELS),
    ("existing_credits", "Existing Credit Lines (this lender)", None),
    ("other_installment_plans", "Other Installment Plans", OTHER_INSTALLMENT_LABELS),
    ("savings_account", "Savings", SAVINGS_LABELS),
    ("employment_since", "Employment Duration", EMPLOYMENT_LABELS),
    ("job", "Job Type", JOB_LABELS),
    ("residence_since", "Residence Duration (years)", None),
    ("housing", "Housing", HOUSING_LABELS),
    ("property", "Property", PROPERTY_LABELS),
    ("age", "Age", None),
    ("personal_status_sex", "Applicant Personal Status", PERSONAL_STATUS_LABELS),
    ("dependents", "Number of Dependents", None),
    ("telephone", "Registered Telephone", TELEPHONE_LABELS),
    ("foreign_worker", "Foreign Worker Status", FOREIGN_WORKER_LABELS),
    ("other_debtors", "Other Debtors / Guarantors", OTHER_DEBTORS_LABELS),
    ("installment_rate", "Installment Commitment", INSTALLMENT_RATE_LABELS),
    ("duration_months", "Loan Tenure (months)", None),
    ("credit_amount", "Loan Amount", None),
    ("purpose", "Purpose", PURPOSE_LABELS),
]


def _parse_application_data(raw):
    """application_data is jsonb; normalize whether the driver already
    deserialized it to a dict or returned the raw JSON text."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw or {}


def _parse_jsonb(raw):
    """Same jsonb normalization as _parse_application_data, but for
    nullable jsonb columns (e.g. assessment_result) where "not present"
    must stay None rather than becoming a fabricated {}."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _humanize_fields(application_data):
    """Renders every one of the 20 submitted fields as a human-readable
    {label, value} row, in the fixed order above. Never includes risk
    score, SHAP, anomaly, or any other model output — this only ever
    sees the raw submitted application_data."""
    rows = []
    for key, label, label_map in FIELD_DISPLAY:
        raw_value = application_data.get(key)
        if label_map is not None:
            value = label_map.get(str(raw_value), raw_value)
        else:
            value = raw_value
        rows.append({"label": label, "value": value if value not in (None, "") else "—"})
    return rows


@lender_bp.route("/lender/login", methods=["GET"])
def lender_login_page():
    """
    Public page — the lender/analyst sign-in screen. Distinct from the
    consumer /login page. Submits credentials to POST /lender/do-login
    (see auth.py), which is the only place role is actually checked.
    """
    return render_template("lender_login.html")


@lender_bp.route("/lender/workspace", methods=["GET"])
@lender_required
def lender_workspace_page():
    """
    Authenticated lender landing page. As of PHASE 2, the page's
    default (landing) view inside this template is the pending
    application queue rather than the manual assessment form — see
    lender_workspace.js / lender_workspace.html.
    """
    return render_template("lender_workspace.html")


# ---------------------------------------------------------------------
# PHASE 2 — lender application queue (read-only)
# ---------------------------------------------------------------------

@lender_bp.route("/lender/applications", methods=["GET"])
@lender_required
def list_lender_applications():
    """
    Returns this lender's PENDING applications only.

    Authorization: lender_id is taken from session["user_id"] (set by
    @lender_required from the DB-backed session), never from the
    request. The WHERE clause below is the entire ownership boundary.
    """
    lender_id = session["user_id"]

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT la.id, la.application_data, la.status, la.created_at,
                   la.assessment_result,
                   u.name AS borrower_name
            FROM loan_applications la
            JOIN users u ON u.id = la.borrower_id
            WHERE la.lender_id = %s AND la.status = 'PENDING'
            ORDER BY la.created_at DESC
            """,
            (lender_id,),
        ).fetchall()
    finally:
        conn.close()

    applications = []
    for row in rows:
        data = _parse_application_data(row["application_data"])
        assessment_result = _parse_jsonb(row["assessment_result"])
        applications.append({
            "application_id": row["id"],
            "applicant_name": row["borrower_name"],
            "requested_amount": data.get("credit_amount"),
            "purpose": PURPOSE_LABELS.get(data.get("purpose"), data.get("purpose")),
            "submitted_at": row["created_at"].isoformat() if row["created_at"] else None,
            "status": row["status"],
            # Compact, persisted-only assessment state for the queue list.
            # No recomputation — straight from assessment_result, or None
            # if the model hasn't been run for this application yet.
            "assessed": assessment_result is not None,
            "decision": assessment_result.get("decision") if assessment_result else None,
        })

    return jsonify({"status": "success", "applications": applications})


@lender_bp.route("/lender/applications/<int:application_id>", methods=["GET"])
@lender_required
def get_lender_application(application_id):
    """
    Returns a single application's read-only details for review.

    Ownership is enforced in the SQL itself (`lender_id = %s`): an
    application that exists but belongs to another lender matches zero
    rows here, and gets the exact same 404 as an id that doesn't exist
    at all — so this endpoint never confirms or denies another
    lender's application ids.

    Returns application/borrower fields plus the persisted core AI
    assessment (assessment_result / assessed_at), if one exists. No
    SHAP, anomaly, scenario, or fairness output is returned here — that
    logic doesn't run here and isn't persisted or in scope for this
    phase. The model is never rerun on this GET; assessment_result is
    whatever was last saved by POST /assess, unchanged.
    """
    lender_id = session["user_id"]

    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id, borrower_id, application_data, status, created_at,
                   assessment_result, assessed_at
            FROM loan_applications
            WHERE id = %s AND lender_id = %s
            """,
            (application_id, lender_id),
        ).fetchone()

        if not row:
            abort(404)

        # Borrower identity always comes from application.borrower_id —
        # never from the lender's own session — per Phase 2 spec.
        borrower = conn.execute(
            "SELECT id, name, email FROM users WHERE id=%s",
            (row["borrower_id"],),
        ).fetchone()
    finally:
        conn.close()

    data = _parse_application_data(row["application_data"])

    application = {
        "application_id": row["id"],
        "status": row["status"],
        "submitted_at": row["created_at"].isoformat() if row["created_at"] else None,
        "borrower": {
            "id": borrower["id"] if borrower else row["borrower_id"],
            "name": borrower["name"] if borrower else None,
            "email": borrower["email"] if borrower else None,
        },
        "loan_amount": data.get("credit_amount"),
        "tenure_months": data.get("duration_months"),
        "purpose": PURPOSE_LABELS.get(data.get("purpose"), data.get("purpose")),
        "fields": _humanize_fields(data),
        # Raw numeric baseline for the Scenario Analysis controls
        # (PHASE 7) — the same four fields SCENARIO_ALLOWED_FIELDS lets
        # a scenario vary. Plain numbers, not display-humanized, since
        # they seed editable number inputs rather than a read-only
        # table.
        "scenario_baseline": {
            "duration_months": data.get("duration_months"),
            "credit_amount": data.get("credit_amount"),
            "installment_rate": data.get("installment_rate"),
            "existing_credits": data.get("existing_credits"),
        },
        # Persisted core AI assessment (Phase 3.5), if this application
        # has one. None if the model hasn't been run yet — the frontend
        # must show the "not yet assessed" empty state in that case, not
        # rerun the model automatically.
        "assessment_result": _parse_jsonb(row["assessment_result"]),
        "assessed_at": row["assessed_at"].isoformat() if row["assessed_at"] else None,
    }

    return jsonify({"status": "success", "application": application})


# ---------------------------------------------------------------------
# PHASE 3 — run the existing credit-risk assessment against the
# application's own stored data (no manual re-entry, no new ML logic).
# ---------------------------------------------------------------------

@lender_bp.route("/lender/applications/<int:application_id>/assess", methods=["POST"])
@lender_required
def assess_lender_application(application_id):
    """
    Runs the verified credit-risk assessment for one application.

    Source of truth: the application's own stored application_data —
    never anything supplied in this request. The frontend only sends
    which application_id to assess.

    Ownership: same pattern as get_lender_application — the SQL filter
    `lender_id = session["user_id"]` is the entire authorization check.
    An application belonging to another lender matches zero rows and
    gets a 404, identical to a nonexistent id.

    Delegates entirely to services.credit_risk_service.assess_credit_risk,
    which is the same verified path used by the existing
    POST /api/credit-risk/assess endpoint. No model, preprocessing,
    threshold, or decision-policy code lives here or is duplicated.
    """
    lender_id = session["user_id"]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, application_data FROM loan_applications WHERE id=%s AND lender_id=%s",
            (application_id, lender_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    applicant = _parse_application_data(row["application_data"])

    result = assess_credit_risk(applicant)

    if not isinstance(result, dict) or result.get("status") != "success":
        # Pass the service's own error through verbatim rather than
        # inventing a message — this is still the backend's verdict,
        # just an error one.
        error_payload = result if isinstance(result, dict) else {
            "status": "error",
            "message": "Assessment failed. Please try again.",
        }
        error_payload.setdefault("application_id", row["id"])
        return jsonify(error_payload), 422

    # ---------------------------------------------------------------
    # PHASE 3.5 — persist the core assessment against this application.
    #
    # Only the fields needed to reconstruct the assessment UI are
    # stored (risk_probability, risk_percentage, risk_level, decision).
    # Nothing is duplicated or fabricated: whatever keys the service
    # didn't return simply aren't stored. SHAP/anomaly/scenario/
    # fairness data is not persisted here — out of scope this phase.
    # ---------------------------------------------------------------
    assessment_to_store = {
        key: result[key]
        for key in ("risk_probability", "risk_percentage", "risk_level", "decision")
        if key in result
    }

    conn = get_db()
    try:
        persisted = conn.execute(
            """
            UPDATE loan_applications
            SET assessment_result = %s, assessed_at = now(), updated_at = now()
            WHERE id = %s AND lender_id = %s
            RETURNING assessed_at
            """,
            (json.dumps(assessment_to_store), row["id"], lender_id),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        persisted = None
    finally:
        conn.close()

    # Return the assessment result verbatim, plus which application it's
    # for. No recomputation or reinterpretation of risk_probability,
    # risk_percentage, risk_level, or decision happens here.
    response_payload = dict(result)
    response_payload["application_id"] = row["id"]
    response_payload["assessed_at"] = persisted["assessed_at"].isoformat() if persisted else None
    return jsonify(response_payload)


# ---------------------------------------------------------------------
# PHASE 4 — decision explanation for the currently selected application.
# ---------------------------------------------------------------------

@lender_bp.route("/lender/applications/<int:application_id>/explain", methods=["POST"])
@lender_required
def explain_lender_application(application_id):
    """
    Returns the SHAP-based decision explanation (risk-increasing /
    risk-reducing factors) for one application.

    Source of truth: the application's own stored application_data —
    never anything supplied in this request. The frontend only sends
    which application_id to explain.

    Ownership: same pattern as assess_lender_application — the SQL
    filter `lender_id = session["user_id"]` is the entire authorization
    check. An application belonging to another lender matches zero
    rows and gets a 404, identical to a nonexistent id — so this
    endpoint never confirms or denies another lender's application ids.

    Delegates entirely to services.credit_risk_service.explain_credit_risk,
    which reuses the existing verified SHAP explanation path. No SHAP,
    model, preprocessing, threshold, or decision-policy code lives here
    or is duplicated.

    Not persisted this phase (per the Phase 4 brief) — recomputed from
    the stored application_data on every call.
    """
    lender_id = session["user_id"]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, application_data FROM loan_applications WHERE id=%s AND lender_id=%s",
            (application_id, lender_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    applicant = _parse_application_data(row["application_data"])

    result = explain_credit_risk(applicant)

    if not isinstance(result, dict) or result.get("status") != "success":
        # Pass the service's own error through verbatim rather than
        # inventing a message.
        error_payload = result if isinstance(result, dict) else {
            "status": "error",
            "message": "Decision explanation is currently unavailable.",
        }
        error_payload.setdefault("application_id", row["id"])
        return jsonify(error_payload), 422

    # Return the explanation verbatim, plus which application it's for.
    # No recomputation or reinterpretation of the SHAP factors happens
    # here.
    response_payload = dict(result)
    response_payload["application_id"] = row["id"]
    return jsonify(response_payload)


# ---------------------------------------------------------------------
# PHASE 5 — borrower evidence + repayment capacity for the currently
# selected application.
#
# CRITICAL: loan_applications.borrower_id is the ONLY source of
# borrower identity below. session["user_id"] identifies the signed-in
# lender (the reviewer) and is used exclusively for the ownership
# filter — it is never passed to the financial-behavior or
# affordability services as the subject.
# ---------------------------------------------------------------------

@lender_bp.route("/lender/applications/<int:application_id>/borrower-evidence", methods=["GET"])
@lender_required
def borrower_evidence_lender_application(application_id):
    """
    Returns the linked borrower's financial-behavior profile for
    underwriting review.

    Ownership: `lender_id = session["user_id"]` in the SQL filter is
    the entire authorization check — an application belonging to
    another lender matches zero rows and gets a 404, identical to a
    nonexistent id.

    Borrower identity: resolved exclusively from
    loan_applications.borrower_id on this row. The lender's own
    session["user_id"] is never used as the borrower.

    Delegates entirely to
    services.financial_behavior_service.get_financial_behavior_profile,
    which already takes an explicit user_id — no changes to that
    service were needed or made. Whether the borrower has enough
    history to be useful is a data_coverage question the frontend
    decides on; this endpoint never fabricates figures or falls back
    to the lender's own transactions.
    """
    lender_id = session["user_id"]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, borrower_id FROM loan_applications WHERE id=%s AND lender_id=%s",
            (application_id, lender_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    borrower_id = row["borrower_id"]

    result = get_financial_behavior_profile(borrower_id)

    if not isinstance(result, dict) or result.get("status") != "success":
        error_payload = result if isinstance(result, dict) else {
            "status": "error",
            "message": "Borrower financial history unavailable",
        }
        error_payload.setdefault("application_id", row["id"])
        return jsonify(error_payload), 422

    response_payload = dict(result)
    response_payload["application_id"] = row["id"]
    return jsonify(response_payload)


@lender_bp.route("/lender/applications/<int:application_id>/repayment-capacity", methods=["POST"])
@lender_required
def repayment_capacity_lender_application(application_id):
    """
    Returns the affordability assessment for this application against
    the linked borrower's actual FinTrust financial capacity.

    Ownership: same `lender_id = session["user_id"]` filter as every
    other lender endpoint here.

    Borrower identity: resolved exclusively from
    loan_applications.borrower_id. The proposed loan parameters
    (credit_amount, duration_months) come from this application's own
    stored application_data — never from the request body.

    Delegates entirely to
    services.affordability_service.calculate_affordability, which
    already takes an explicit user_id — no changes to that service or
    the underlying calculation were needed or made.
    """
    lender_id = session["user_id"]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, borrower_id, application_data FROM loan_applications WHERE id=%s AND lender_id=%s",
            (application_id, lender_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    borrower_id = row["borrower_id"]
    applicant = _parse_application_data(row["application_data"])

    result = calculate_affordability(borrower_id, applicant)

    if not isinstance(result, dict) or result.get("status") != "success":
        error_payload = result if isinstance(result, dict) else {
            "status": "error",
            "message": "Repayment capacity unavailable",
        }
        error_payload.setdefault("application_id", row["id"])
        return jsonify(error_payload), 422

    response_payload = dict(result)
    response_payload["application_id"] = row["id"]
    return jsonify(response_payload)


# ---------------------------------------------------------------------
# PHASE 6 — application anomaly for the currently selected application.
# ---------------------------------------------------------------------

@lender_bp.route("/lender/applications/<int:application_id>/anomaly", methods=["POST"])
@lender_required
def anomaly_lender_application(application_id):
    """
    Runs the verified anomaly check for one application.

    Source of truth: the application's own stored application_data —
    never anything supplied in this request. The frontend only sends
    which application_id to check.

    Ownership: same pattern as assess_lender_application /
    explain_lender_application — the SQL filter
    `lender_id = session["user_id"]` is the entire authorization check.
    An application belonging to another lender matches zero rows and
    gets a 404, identical to a nonexistent id.

    Delegates entirely to
    services.credit_risk_service.check_credit_anomaly, which wraps the
    same canonical, offline-fitted Isolation Forest anomaly path used
    elsewhere. No anomaly model, preprocessing, threshold, or algorithm
    code lives here or is duplicated, and nothing is fit or retrained
    on this request.

    Not persisted this phase (per the Phase 6 brief) — recomputed from
    the stored application_data on every call.
    """
    lender_id = session["user_id"]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, application_data FROM loan_applications WHERE id=%s AND lender_id=%s",
            (application_id, lender_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    applicant = _parse_application_data(row["application_data"])

    result = check_credit_anomaly(applicant)

    if not isinstance(result, dict) or result.get("status") != "success":
        # Pass the service's own error through verbatim rather than
        # inventing a message.
        error_payload = result if isinstance(result, dict) else {
            "status": "error",
            "message": "Application anomaly analysis is currently unavailable.",
        }
        error_payload.setdefault("application_id", row["id"])
        return jsonify(error_payload), 422

    # Return the anomaly result verbatim, plus which application it's
    # for. No recomputation or reinterpretation of is_anomaly,
    # anomaly_score, anomaly_level, or manual_review happens here.
    response_payload = dict(result)
    response_payload["application_id"] = row["id"]
    return jsonify(response_payload)


# ---------------------------------------------------------------------
# PHASE 7 — scenario analysis for the currently selected application.
#
# Baseline is always the application's own stored application_data —
# never lwCurrentApplicant, never anything the client supplies. The
# request may only carry scenario changes (a small whitelist of loan
# variables); it can never replace the baseline applicant payload.
# Scenario Analysis is exploratory: this endpoint never writes to
# loan_applications.application_data, assessment_result, status, or
# any other persisted field.
# ---------------------------------------------------------------------

@lender_bp.route("/lender/applications/<int:application_id>/scenario", methods=["POST"])
@lender_required
def scenario_lender_application(application_id):
    """
    Runs a what-if scenario against one application's stored data.

    Ownership: same `lender_id = session["user_id"]` filter as every
    other lender endpoint here. An application belonging to another
    lender matches zero rows and gets a 404, identical to a
    nonexistent id — so this endpoint never confirms or denies another
    lender's application ids.

    Baseline: the application's own stored application_data, loaded
    fresh from the database on every call. The client-supplied request
    body is filtered down to SCENARIO_ALLOWED_FIELDS before use, so it
    can only nudge those specific values — it can never substitute a
    different baseline applicant.

    Delegates entirely to
    services.credit_risk_service.analyze_credit_scenario, which is the
    same verified scenario path used by the existing
    POST /api/credit-risk/scenario endpoint. No scenario, model,
    preprocessing, threshold, or decision-policy code lives here or is
    duplicated, and nothing about this request is persisted —
    application_data, assessment_result, and status are all left
    untouched.
    """
    lender_id = session["user_id"]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, application_data FROM loan_applications WHERE id=%s AND lender_id=%s",
            (application_id, lender_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    applicant = _parse_application_data(row["application_data"])

    body = request.get_json(silent=True) or {}
    raw_changes = body.get("changes") if isinstance(body.get("changes"), dict) else {}
    changes = {
        key: value
        for key, value in raw_changes.items()
        if key in SCENARIO_ALLOWED_FIELDS
    }

    result = analyze_credit_scenario(applicant, changes)

    # analyze_credit_scenario() returns analyze_scenario()'s normalized
    # result as-is, which carries no "status" key of its own — that
    # convention (adding status="success" on the way out) belongs to
    # the route layer, same as the existing POST /api/credit-risk/scenario
    # endpoint. A dict without "errors" is a valid result; anything else
    # (not a dict, or a dict carrying "errors") is an actual failure.
    if not isinstance(result, dict) or "errors" in result:
        # Pass the service's own error through verbatim rather than
        # inventing a message.
        error_payload = result if isinstance(result, dict) else {
            "message": "Scenario analysis is currently unavailable.",
        }
        error_payload.setdefault("status", "error")
        error_payload.setdefault("application_id", row["id"])
        return jsonify(error_payload), 422

    result["status"] = "success"

    # Return the scenario result verbatim, plus which application it's
    # for. No recomputation or reinterpretation of baseline/scenario
    # probabilities, risk levels, decisions, delta, or interpretation
    # happens here.
    response_payload = dict(result)
    response_payload["application_id"] = row["id"]
    return jsonify(response_payload)


# ---------------------------------------------------------------------
# PHASE 8 — the lender's FINAL lending decision.
#
# This is the only place loan_applications.status changes after a
# borrower submits it. The AI decision (assessment_result.decision) is
# advisory only — it is never read, required, or auto-applied here.
# The lender explicitly chooses APPROVED, REJECTED, or PENDING; nothing
# about application_data, the AI assessment, or ownership is touched.
# ---------------------------------------------------------------------

# The only values a lender may set as the final application status.
FINAL_DECISION_STATUSES = {"APPROVED", "REJECTED", "PENDING"}

# Once an application reaches one of these, it is finalized and this
# endpoint must refuse to change it again rather than silently
# overwriting a prior decision. WITHDRAWN is included here even though
# a lender can never *set* it (see FINAL_DECISION_STATUSES above) —
# once a borrower withdraws, the application is just as terminal from
# the lender's side as an APPROVED/REJECTED one.
_FINALIZED_STATUSES = {"APPROVED", "REJECTED", "WITHDRAWN"}


@lender_bp.route("/lender/applications/<int:application_id>/decision", methods=["POST"])
@lender_required
def decide_lender_application(application_id):
    """
    Records the lender's final decision (APPROVED / REJECTED / PENDING)
    for one application.

    Ownership: identical pattern to every other endpoint in this file —
    `id = %s AND lender_id = session["user_id"]` is the entire
    authorization boundary. An application belonging to another lender
    matches zero rows and gets a 404, identical to a nonexistent id, so
    this endpoint never confirms or denies another lender's application
    ids.

    The request body may only carry `decision`. borrower_id, lender_id,
    and application_data are never read from the client here — the URL
    path segment is the only application identity used, and the lender
    identity comes only from the session.

    Only the loan_applications.status and updated_at columns are
    written. application_data, assessment_result, and assessed_at are
    left completely untouched — this endpoint never reruns or
    reinterprets the AI assessment.

    An application already APPROVED, REJECTED, or WITHDRAWN cannot be
    silently overwritten; the UPDATE is scoped to non-finalized rows,
    and a finalized row is reported back as a conflict rather than
    changed. A borrower-withdrawn application is terminal here too — a
    lender can never approve or reject an application the borrower has
    withdrawn.
    """
    lender_id = session["user_id"]

    body = request.get_json(silent=True)
    decision = body.get("decision") if isinstance(body, dict) else None
    decision = decision.strip().upper() if isinstance(decision, str) else None

    if decision not in FINAL_DECISION_STATUSES:
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Decision must be one of: APPROVED, REJECTED, PENDING."],
        }), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, status FROM loan_applications WHERE id=%s AND lender_id=%s",
            (application_id, lender_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    if row["status"] in _FINALIZED_STATUSES:
        return jsonify({
            "status": "error",
            "error_type": "conflict",
            "errors": ["This application has already been finalized." if row["status"] != "WITHDRAWN"
                       else "This application was withdrawn by the borrower and can no longer be decided."],
        }), 409

    # Audit metadata: only APPROVED/REJECTED are final lender decisions
    # and get decided_at/decided_by. PENDING is not a decision — it
    # explicitly clears any prior audit metadata (matching the existing
    # rule that PENDING rows carry no decision state).
    if decision in _FINALIZED_STATUSES:
        decided_at_expr = "now()"
        decided_by_value = lender_id
    else:
        decided_at_expr = "NULL"
        decided_by_value = None

    conn = get_db()
    try:
        updated = conn.execute(
            f"""
            UPDATE loan_applications
            SET status = %s, updated_at = now(),
                decided_at = {decided_at_expr}, decided_by = %s
            WHERE id = %s AND lender_id = %s AND status NOT IN ('APPROVED', 'REJECTED', 'WITHDRAWN')
            RETURNING id, status, updated_at, decided_at, decided_by
            """,
            (decision, decided_by_value, application_id, lender_id),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Failed to record decision. Please try again."],
        }), 500
    finally:
        conn.close()

    if not updated:
        # Finalized (approved/rejected) or withdrawn by a concurrent
        # request between the check above and this UPDATE — report the
        # same conflict rather than a fabricated success.
        return jsonify({
            "status": "error",
            "error_type": "conflict",
            "errors": ["This application can no longer be decided."],
        }), 409

    return jsonify({
        "status": "success",
        "application_id": updated["id"],
        "application_status": updated["status"],
        "updated_at": updated["updated_at"].isoformat() if updated["updated_at"] else None,
        "decided_at": updated["decided_at"].isoformat() if updated["decided_at"] else None,
        "decided_by": updated["decided_by"],
    })