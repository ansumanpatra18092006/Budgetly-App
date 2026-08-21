# routes/credit_risk.py

"""
Credit Risk Assessment Routes
Exposes canonical endpoints for model evaluation, explainability, 
scenario analysis, and responsible AI monitoring.
"""

from flask import Blueprint, request, jsonify, session
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

@credit_risk_bp.route("/financial-behavior", methods=["GET"])
@login_required
def financial_behavior():
    try:
        profile = get_financial_behavior_profile(session.get("user_id"))
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
    Evaluates loan affordability for an applicant purely based on Budgetly cash-flow.
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

    try:
        result = calculate_affordability(session.get("user_id"), applicant)
        return jsonify(result), 200
    except Exception:
        return jsonify({
            "status": "error",
            "error_type": "internal_error",
            "errors": ["Affordability assessment failed unexpectedly."]
        }), 500