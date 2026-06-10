"""
WhatsApp Meal Survey — Flask Application
=========================================
A Python Flask app that sends meal satisfaction surveys via WhatsApp
and collects feedback with these key features:

1. ONE response per user — duplicates are blocked at multiple levels
2. 4 rating options: Very Good, Good, Satisfactory, Not Acceptable
3. When "Not Acceptable" is selected, user can type free-text complaint
4. Frontend dashboard shows all responses in real-time
5. Data persisted to local JSON files

Deploy to Render with environment variables:
- WHATSAPP_TOKEN
- WHATSAPP_PHONE_NUMBER_ID
- WHATSAPP_VERIFY_TOKEN
- WHATSAPP_API_VERSION (default: v21.0)
- WHATSAPP_TEMPLATE_NAME (default: meal_survey)
- WHATSAPP_TEMPLATE_LANGUAGE (default: en_US)
"""

import os
from flask import Flask, request, jsonify, render_template
import store
from whatsapp_service import WhatsAppService
from conversation import process_webhook, mark_phone_completed, unmark_phone_completed

app = Flask(__name__)

# Initialize storage on startup
store.init()

# WhatsApp service instance
wa = WhatsAppService()

# Recent webhook payloads for debugging
_recent_webhooks = []
MAX_WEBHOOK_LOG = 20


@app.route("/")
def index():
    """Serve the frontend dashboard."""
    return render_template("index.html")


# ── Webhook endpoints ─────────────────────────────────────────────────


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """WhatsApp webhook verification (GET).
    
    WhatsApp sends: hub.mode=subscribe&hub.verify_token=<your_token>&hub.challenge=<string>
    You MUST include hub.mode=subscribe for verification to succeed.
    
    Test URL example:
      /webhook?hub.mode=subscribe&hub.verify_token=my_verify_token&hub.challenge=test123
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "my_verify_token")

    print(f"[webhook] GET verify — mode={mode}, token={token}, challenge={challenge}, expected_token={verify_token}")

    # WhatsApp requires hub.mode=subscribe for verification
    if mode == "subscribe" and token == verify_token:
        print("[webhook] Verification succeeded")
        return challenge, 200

    # Also allow verification without hub.mode for simpler testing
    # (Meta always sends hub.mode=subscribe, but for quick manual tests this is convenient)
    if token == verify_token and challenge:
        print("[webhook] Verification succeeded (no hub.mode, token matched)")
        return challenge, 200

    print(f"[webhook] Verification FAILED — received mode={mode}, token={token}")
    print(f"[webhook] Expected: hub.mode=subscribe, hub.verify_token={verify_token}")
    return "Forbidden — ensure hub.mode=subscribe and hub.verify_token match your WHATSAPP_VERIFY_TOKEN env var", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    """WhatsApp webhook message receiver (POST)."""
    body = request.get_json(silent=True) or {}
    print(f"[webhook] POST received")

    # Save raw payload for debugging
    _recent_webhooks.append(body)
    if len(_recent_webhooks) > MAX_WEBHOOK_LOG:
        _recent_webhooks.pop(0)

    try:
        process_webhook(body, wa)
    except Exception as e:
        print(f"[webhook] Error processing payload: {e}")

    # Always return 200 quickly — WhatsApp requires fast acknowledgment
    return "OK", 200


# ── Survey API ────────────────────────────────────────────────────────


@app.route("/api/survey/send", methods=["POST"])
def send_survey():
    """Send a meal survey to a WhatsApp user."""
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    meal_name = data.get("mealName", "").strip()
    survey_type = (data.get("surveyType") or "list").lower()

    if not phone or not meal_name:
        return jsonify({"error": "Phone and MealName are required."}), 400

    # Set user state so we can track it through the conversation
    store.set_state(phone, "SURVEY_SENT", meal_name)

    try:
        print(f"[survey] Sending {survey_type} survey to {phone} for {meal_name}")
        resp = wa.send_survey(phone, meal_name, survey_type)
        return jsonify({
            "status": "sent",
            "phone": phone,
            "mealName": meal_name,
            "surveyType": survey_type,
        })
    except Exception as e:
        print(f"[survey] WhatsApp API error: {e}")
        return jsonify({"error": str(e)}), 400


# ── UI API ────────────────────────────────────────────────────────────


@app.route("/api/ui/recent")
def get_recent():
    """Get recent feedback entries."""
    try:
        recent = store.get_recent(50)
        return jsonify(recent)
    except Exception as e:
        print(f"[ui] Error fetching recent feedback: {e}")
        return jsonify({"error": "Failed to load feedback", "details": str(e)}), 500


@app.route("/api/ui/count")
def get_count():
    """Get total feedback count."""
    recent = store.get_recent(1000)
    return jsonify({"count": len(recent)})


@app.route("/api/ui/reset/<phone>", methods=["DELETE"])
def reset_user(phone):
    """Reset all feedback and state for a phone number (testing)."""
    print(f"[ui] Resetting user {phone}")
    store.reset_user(phone)
    unmark_phone_completed(phone)
    return jsonify({"status": "reset", "phone": phone})


@app.route("/api/ui/debug/webhooks")
def get_debug_webhooks():
    """Debug endpoint — returns recent raw webhook payloads."""
    return jsonify({"count": len(_recent_webhooks), "webhooks": _recent_webhooks})


# ── Run ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[app] Starting WhatsApp Meal Survey on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
