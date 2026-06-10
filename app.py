"""
WhatsApp Meal Survey — Flask Application
=========================================
Key features:
1. ONE response per user per day — date-based duplicate blocking
2. 4 rating options: Very Good, Good, Satisfactory, Not Acceptable
3. When "Not Acceptable" is selected, user can type free-text complaint
4. Frontend dashboard shows all responses in real-time
5. Multi-phone survey sending with date + meal selection
6. Data persisted to local JSON files

Environment variables:
- WHATSAPP_TOKEN
- WHATSAPP_PHONE_NUMBER_ID
- WHATSAPP_VERIFY_TOKEN
"""

import os
import re
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template
import store
from whatsapp_service import WhatsAppService
from conversation import process_webhook, unmark_all_for_phone

app = Flask(__name__)

store.init()
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


def _parse_phones(phones_input) -> list[str]:
    """Parse phone numbers from various input formats.
    Accepts: comma-separated string, list of strings, or single string.
    Returns list of cleaned phone numbers.
    """
    if isinstance(phones_input, list):
        phones = []
        for p in phones_input:
            phones.extend([x.strip() for x in str(p).split(",") if x.strip()])
        return phones
    if isinstance(phones_input, str):
        return [x.strip() for x in phones_input.split(",") if x.strip()]
    return []


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
    """Send a meal survey to one or more WhatsApp users.

    Request body:
    {
        "phones": "923001234567,923009876543" or ["923001234567", "923009876543"],
        "mealName": "Chicken Biryani",
        "surveyDate": "2026-06-10",  // optional, defaults to today UTC
        "surveyType": "list"          // optional, defaults to "list"
    }

    The surveyDate is validated (not future, proper format) and stored.
    It is NOT shown to the WhatsApp user.
    """
    data = request.get_json(silent=True) or {}
    meal_name = data.get("mealName", "").strip()
    survey_type = (data.get("surveyType") or "list").lower()
    survey_date = data.get("surveyDate", "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Parse phones (accepts comma-separated string or list)
    phones = _parse_phones(data.get("phones", data.get("phone", "")))

    if not phones:
        return jsonify({"error": "At least one phone number is required."}), 400
    if not meal_name:
        return jsonify({"error": "Meal name is required."}), 400

    # Validate date
    if not _validate_date(survey_date):
        return jsonify({"error": f"Invalid survey date '{survey_date}'. Use YYYY-MM-DD format, not in the future."}), 400

    results = []
    errors = []

    for phone in phones:
        if not phone:
            continue

        # Set user state with surveyDate — this allows them to respond
        store.set_state(phone, "SURVEY_SENT", meal_name, survey_date)

        try:
            print(f"[survey] Sending {survey_type} survey to {phone} for {meal_name} (date: {survey_date})")
            wa.send_survey(phone, meal_name, survey_type)
            results.append({"phone": phone, "status": "sent"})
        except Exception as e:
            print(f"[survey] Failed to send to {phone}: {e}")
            errors.append({"phone": phone, "error": str(e)})

    # Log the survey batch
    store.add_survey({
        "mealName": meal_name,
        "surveyDate": survey_date,
        "surveyType": survey_type,
        "phones": phones,
        "sentCount": len(results),
        "errorCount": len(errors),
    })

    return jsonify({
        "status": "completed",
        "mealName": meal_name,
        "surveyDate": survey_date,
        "surveyType": survey_type,
        "sent": results,
        "errors": errors,
    })


# ── UI API ────────────────────────────────────────────────────────────


@app.route("/api/ui/recent")
def get_recent():
    try:
        recent = store.get_recent(50)
        return jsonify(recent)
    except Exception as e:
        print(f"[ui] Error fetching recent feedback: {e}")
        return jsonify({"error": "Failed to load feedback", "details": str(e)}), 500


@app.route("/api/ui/surveys")
def get_surveys():
    """Get recent survey batches."""
    try:
        surveys = store.get_surveys(20)
        return jsonify(surveys)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ui/count")
def get_count():
    recent = store.get_recent(1000)
    return jsonify({"count": len(recent)})


@app.route("/api/ui/reset/<phone>", methods=["DELETE"])
def reset_user(phone):
    print(f"[ui] Resetting user {phone}")
    store.reset_user(phone)
    unmark_all_for_phone(phone)
    return jsonify({"status": "reset", "phone": phone})


@app.route("/api/ui/debug/webhooks")
def get_debug_webhooks():
    return jsonify({"count": len(_recent_webhooks), "webhooks": _recent_webhooks})


@app.route("/api/ui/debug/state")
def get_debug_state():
    """Debug endpoint — show all current user states."""
    return jsonify({
        "stateKeys": store.get_all_state_keys(),
        "states": {k: store.get_state(k) for k in store.get_all_state_keys()},
    })


# ── Run ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[app] Starting WhatsApp Meal Survey on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
