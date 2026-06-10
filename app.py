"""
WhatsApp Meal Survey — Flask Application (Supabase Backend)
===========================================================
Key features:
1. ONE response per user per date (UNIQUE constraint in Supabase)
2. 3-button WhatsApp survey — buttons disappear after tap (no duplicates!)
3. "Not Acceptable" → free-text remarks flow
4. Send surveys only to users with availstatus = true
5. All data in Supabase (no JSON files, no in-memory state loss)
6. User management from UI

Environment variables:
- WHATSAPP_TOKEN
- WHATSAPP_PHONE_NUMBER_ID
- WHATSAPP_VERIFY_TOKEN
- SUPABASE_URL
- SUPABASE_SERVICE_KEY
"""

import os
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template
from store import store as db
from whatsapp_service import WhatsAppService
from conversation import process_webhook

app = Flask(__name__)

db.init()
wa = WhatsAppService()

_recent_webhooks = []
MAX_WEBHOOK_LOG = 20


# ── Helpers ───────────────────────────────────────────────────────────


def _validate_date(date_str: str) -> bool:
    """Validate YYYY-MM-DD format and it's not in the future."""
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        today = datetime.now(timezone.utc).date()
        if parsed.date() > today:
            return False
        return True
    except ValueError:
        return False


def _clean_phone(phone: str) -> str:
    """Clean a phone number — strip + and spaces."""
    return phone.strip().replace("+", "").replace(" ", "")


# ── Routes ────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Serve the frontend dashboard — intercept WhatsApp verification too."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if token and challenge:
        verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "my_verify_token")
        print(f"[root] Webhook verify — mode={mode}, token={token}, challenge={challenge}")
        if (mode == "subscribe" and token == verify_token) or (token == verify_token and challenge):
            print("[root] Verification succeeded")
            return challenge, 200
        print("[root] Verification FAILED")
        return "Forbidden", 403

    return render_template("index.html")


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "my_verify_token")

    print(f"[webhook] GET verify — mode={mode}, token={token}")

    if (mode == "subscribe" and token == verify_token) or (token == verify_token and challenge):
        print("[webhook] Verification succeeded")
        return challenge, 200

    return "Forbidden", 403


@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook_receive():
    body = request.get_json(silent=True) or {}
    print(f"[webhook] POST received")

    _recent_webhooks.append(body)
    if len(_recent_webhooks) > MAX_WEBHOOK_LOG:
        _recent_webhooks.pop(0)

    try:
        process_webhook(body, wa)
    except Exception as e:
        print(f"[webhook] Error processing payload: {e}")

    return "OK", 200


# ── Survey API ────────────────────────────────────────────────────────


@app.route("/api/survey/send", methods=["POST"])
def send_survey():
    """Send a meal survey to all available users.

    Request body:
    {
        "mealName": "Chicken Biryani",
        "surveyDate": "2026-06-10",
        "surveyType": "button"   // optional, defaults to "button"
    }

    Sends to ALL users with availstatus = true.
    Creates a meal entry and pending response rows in Supabase.
    """
    data = request.get_json(silent=True) or {}
    meal_name = data.get("mealName", "").strip()
    survey_type = (data.get("surveyType") or "button").lower()
    survey_date = data.get("surveyDate", "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not meal_name:
        return jsonify({"error": "Meal name is required."}), 400

    if not _validate_date(survey_date):
        return jsonify({"error": f"Invalid survey date '{survey_date}'. Use YYYY-MM-DD, not in the future."}), 400

    # Get available users
    available_users = db.get_available_users()
    if not available_users:
        return jsonify({"error": "No available users found. Add users with availstatus=true."}), 400

    # Create meal entry
    try:
        db.add_meal(survey_date, meal_name)
    except Exception as e:
        print(f"[survey] Meal entry error (may already exist): {e}")

    results = []
    errors = []

    for user in available_users:
        phone = user["phoneno"]
        user_id = user["id"]
        user_name = user.get("name", "")

        # Create pending response (skips if already exists)
        response = db.create_response(user_id, survey_date, meal_name)
        if not response:
            results.append({"phone": phone, "name": user_name, "status": "skipped", "reason": "already has response"})
            continue

        try:
            print(f"[survey] Sending {survey_type} survey to {user_name} ({phone}) for {meal_name} (date: {survey_date})")
            wa.send_survey(phone, meal_name, survey_type)
            results.append({"phone": phone, "name": user_name, "status": "sent"})
        except Exception as e:
            print(f"[survey] Failed to send to {phone}: {e}")
            errors.append({"phone": phone, "name": user_name, "error": str(e)})

    return jsonify({
        "status": "completed",
        "mealName": meal_name,
        "surveyDate": survey_date,
        "surveyType": survey_type,
        "totalAvailable": len(available_users),
        "sent": results,
        "errors": errors,
    })


# ── User Management API ──────────────────────────────────────────────


@app.route("/api/users", methods=["GET"])
def get_users():
    """Get all users."""
    try:
        users = db.get_all_users()
        return jsonify(users)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users", methods=["POST"])
def add_user():
    """Add a new user."""
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    phoneno = data.get("phoneno", "").strip()
    availstatus = data.get("availstatus", True)

    if not name or not phoneno:
        return jsonify({"error": "Name and phone number are required."}), 400

    try:
        user = db.add_user(name, phoneno, availstatus)
        return jsonify(user), 201
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower() or "409" in str(e):
            return jsonify({"error": f"Phone number {phoneno} already exists."}), 409
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<user_id>", methods=["PATCH"])
def update_user(user_id):
    """Update a user (name, phoneno, availstatus)."""
    data = request.get_json(silent=True) or {}
    try:
        user = db.update_user(user_id, **data)
        return jsonify(user)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<user_id>", methods=["DELETE"])
def delete_user(user_id):
    """Delete a user."""
    try:
        db.delete_user(user_id)
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/available", methods=["GET"])
def get_available_users():
    """Get available users (availstatus=true)."""
    try:
        users = db.get_available_users()
        return jsonify(users)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Response / UI API ────────────────────────────────────────────────


@app.route("/api/ui/recent")
def get_recent():
    try:
        recent = db.get_recent(50)
        return jsonify(recent)
    except Exception as e:
        print(f"[ui] Error fetching recent: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ui/surveys")
def get_surveys():
    try:
        surveys = db.get_surveys(20)
        return jsonify(surveys)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ui/count")
def get_count():
    recent = db.get_recent(1000)
    return jsonify({"count": len(recent)})


@app.route("/api/ui/reset/<phone>", methods=["DELETE"])
def reset_user(phone):
    print(f"[ui] Resetting user {phone}")
    db.reset_user(phone)
    return jsonify({"status": "reset", "phone": phone})


@app.route("/api/ui/debug/webhooks")
def get_debug_webhooks():
    return jsonify({"count": len(_recent_webhooks), "webhooks": _recent_webhooks})


@app.route("/api/ui/debug/state")
def get_debug_state():
    """Debug endpoint — show active response states."""
    try:
        keys = db.get_all_state_keys()
        return jsonify({"activePhones": keys, "count": len(keys)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Run ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[app] Starting WhatsApp Meal Survey on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
