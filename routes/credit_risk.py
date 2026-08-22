# routes/credit_risk.py

"""
Credit Risk Assessment Routes
Exposes canonical endpoints for model evaluation, explainability, 
scenario analysis, and responsible AI monitoring.
"""

from flask import Blueprint, request, jsonify
from utils.decorators import login_required
from services.credit_risk_service import (
    assess_credit_risk,
    explain_credit_risk,
    analyze_credit_scenario,
    get_responsible_ai_data,
    check_credit_anomaly
)
from services.financial_behavior_service import get_financial_behavior_profile
from services.affordability_service import calculate_affordability

credit_risk_bp = Blueprint("credit_risk", __name__, url_prefix="/api/credit-risk")

@credit_risk_bp.route("/assess", methods=["POST"])
@login_required
def assess():
    payload = request.get_json(silent=True)

    if payload is None or not isinstance(payload, dict):
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Request body must be a JSON object."],
        }), 400

    try:
        result = assess_credit_risk(payload)
    except Exception:
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Credit risk assessment failed unexpectedly."],
        }), 500

    if result.get("status") == "error":
        return jsonify(result), 400

    return jsonify(result), 200

@credit_risk_bp.route("/explain", methods=["POST"])
@login_required
def explain():
    payload = request.get_json(silent=True)

    if payload is None or not isinstance(payload, dict):
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Request body must be a JSON object."],
        }), 400

    try:
        result = explain_credit_risk(payload)
    except Exception:
        return jsonify({
            "status": "success",
            "explanation_available": False,
            "message": "Explanation generation failed unexpectedly.",
        }), 200

    if result.get("status") == "error":
        return jsonify(result), 400

    return jsonify(result), 200

@credit_risk_bp.route("/scenario", methods=["POST"])
@login_required
def scenario():
    payload = request.get_json(silent=True)

    if payload is None or not isinstance(payload, dict):
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Request body must be a JSON object."],
        }), 400
        
    applicant = payload.get("applicant")
    changes = payload.get("changes")

    if not applicant or not isinstance(applicant, dict) or not isinstance(changes, dict):
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Request must include valid 'applicant' and 'changes' objects."],
        }), 400

    try:
        result = analyze_credit_scenario(applicant, changes)
        result["status"] = "success"
        return jsonify(result), 200
    except ValueError as ve:
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": [str(ve)],
        }), 400
    except Exception:
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Scenario analysis failed unexpectedly."],
        }), 500

@credit_risk_bp.route("/responsible-ai", methods=["GET"])
@login_required
def responsible_ai():
    try:
        data = get_responsible_ai_data()
        return jsonify(data), 200
    except Exception:
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Responsible AI data is currently unavailable."]
        }), 500

@credit_risk_bp.route("/anomaly", methods=["POST"])
@login_required
def anomaly():
    payload = request.get_json(silent=True)

    if payload is None or not isinstance(payload, dict):
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Request body must be a JSON object."],
        }), 400

    try:
        result = check_credit_anomaly(payload)
    except Exception:
        return jsonify({
            "status": "success",
            "available": False,
            "message": "Anomaly check failed unexpectedly."
        }), 200

    if result.get("status") == "error":
        return jsonify(result), 400

    return jsonify(result), 200

def _resolve_borrower_id(candidate_id):
    """
    Data-ownership boundary for lender-facing borrower data.

    The lender-side workspace must NEVER read the authenticated lender's own
    Budgetly data as a stand-in for the borrower being assessed. Previously
    these endpoints called session.get("user_id"), which is the logged-in
    LENDER's id, not the borrower's — that was incorrect and has been removed.

    This app does not yet have a verified borrower/application relationship
    (e.g. borrower consent, an application record linking a lender's
    assessment to a specific borrower account). Until that relationship
    exists, we deliberately do NOT resolve a client-supplied borrower_id to
    a real user record here — doing so would just trade one incorrect data
    source (the lender's own session) for another unverified one (trusting
    any id the client sends). Both fail to demonstrate a real borrower link.

    Returns None in all cases for this phase. The caller is expected to
    surface that as an explicit "not linked" result rather than silently
    substituting any other user's financial data.
    """
    return None


@credit_risk_bp.route("/financial-behavior", methods=["GET"])
@login_required
def financial_behavior():
    borrower_id = _resolve_borrower_id(request.args.get("borrower_id"))

    if not borrower_id:
        return jsonify({
            "status": "success",
            "borrower_linked": False,
            "message": (
                "No verified borrower is linked to this application, so borrower "
                "financial evidence cannot be shown. This workspace does not use "
                "the signed-in lender's own financial data as a substitute for "
                "borrower evidence."
            )
        }), 200

    try:
        profile = get_financial_behavior_profile(borrower_id)
        return jsonify(profile), 200
    except Exception:
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Failed to calculate financial behavior profile."]
        }), 500

@credit_risk_bp.route("/affordability", methods=["POST"])
@login_required
def affordability():
    """
    Evaluates loan affordability for an applicant purely based on the
    BORROWER's Budgetly cash-flow — never the signed-in lender's own data.
    """
    payload = request.get_json(silent=True)
    if payload is None or not isinstance(payload, dict):
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Request body must be a JSON object."]
        }), 400
    
    applicant = payload.get("applicant")
    if not applicant or not isinstance(applicant, dict):
        return jsonify({
            "status": "error",
            "error_type": "validation_error",
            "errors": ["Request must include valid 'applicant' object."]
        }), 400

    borrower_id = _resolve_borrower_id(payload.get("borrower_id"))

    if not borrower_id:
        return jsonify({
            "status": "success",
            "borrower_linked": False,
            "message": (
                "No verified borrower is linked to this application, so repayment "
                "capacity cannot be calculated. This workspace does not use the "
                "signed-in lender's own financial data as a substitute for "
                "borrower evidence."
            )
        }), 200

    try:
        result = calculate_affordability(borrower_id, applicant)
        return jsonify(result), 200
    except Exception:
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Affordability assessment failed unexpectedly."]
        }), 500