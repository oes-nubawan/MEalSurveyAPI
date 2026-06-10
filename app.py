"""
WhatsApp Meal Survey — Flask Application (Supabase Backend)
===========================================================
Key features:
1. ONE response per user per date (UNIQUE constraint in Supabase)
2. 3-button WhatsApp survey — buttons disappear after tap (no duplicates!)
3. "Not Acceptable" → free-text remarks flow
4. Send surveys only to users who availed the meal (tbl_mealavail per-date status)
5. All data in Supabase (no JSON files, no in-memory state loss)
6. User management from UI
7. Auto-send via external cron (cron-job.org) — works on Render free tier

NO APScheduler — Render free tier sleeps after 15 min of inactivity,
so in-process schedulers are useless. Instead, use a free external cron
service to hit /api/cron/daily-survey at 2:30 PM PKT daily.

Environment variables:
- WHATSAPP_TOKEN
- WHATSAPP_PHONE_NUMBER_ID
- WHATSAPP_VERIFY_TOKEN
- SUPABASE_URL
- SUPABASE_SERVICE_KEY
- CRON_SECRET (required — secret token to protect the cron endpoint)
"""

import os
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template
from store import store as db
from whatsapp_service import WhatsAppService
from conversation import process_webhook

app = Flask(__name__)

db.init()
wa = WhatsAppService()

_recent_webhooks = []
MAX_WEBHOOK_LOG = 20

# ── Auto-send log (for UI display) ────────────────────────────────────
_auto_send_log = []
MAX_AUTO_LOG = 20


# ── Helpers ───────────────────────────────────────────────────────────

# Pakistan timezone = UTC+5 (no pytz needed — saves a dependency)
_PKT_OFFSET = timedelta(hours=5)


def _pkt_now():
    """Current datetime in Pakistan timezone (UTC+5)."""
    return datetime.now(timezone.utc) + _PKT_OFFSET


def _pkt_today():
    """Today's date string in Pakistan timezone."""
    return _pkt_now().strftime("%Y-%m-%d")


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
    """Send a meal survey to users who availed the meal on the selected date.

    If no meal exists in tbl_meal for the selected date → HOLIDAY — no survey.
    """
    data = request.get_json(silent=True) or {}
    meal_name = data.get("mealName", "").strip()
    survey_type = (data.get("surveyType") or "button").lower()
    survey_date = data.get("surveyDate", "").strip() or _pkt_today()

    if not _validate_date(survey_date):
        return jsonify({"error": f"Invalid survey date '{survey_date}'. Use YYYY-MM-DD, not in the future."}), 400

    # Check if meal exists for this date — if not, it's a holiday (no survey)
    meal_entry = db.get_meal_by_date(survey_date)
    if meal_entry:
        meal_name = meal_entry.get("meal_name", meal_name)
        print(f"[survey] Meal for {survey_date}: {meal_name}")
    else:
        return jsonify({"error": f"No meal registered for {survey_date}. This is a holiday — no tokens, no meal, no survey."}), 400

    if not meal_name:
        return jsonify({"error": "Meal name is required. Ensure a meal is registered for the selected date."}), 400

    # Get users who availed the meal on this date (from tbl_mealavail)
    available_users = db.get_available_users_for_date(survey_date)
    if not available_users:
        return jsonify({"error": "No users have availed the meal for this date. Mark users as availed in the Users tab."}), 400

    # Create meal entry (idempotent)
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


# ── Cron Webhook (external cron triggers this) ────────────────────────


@app.route("/api/cron/daily-survey", methods=["GET", "POST"])
def cron_daily_survey():
    """Lightweight endpoint for external cron (cron-job.org) to call at 2:30 PM PKT.

    Protected by CRON_SECRET query param: ?secret=your_secret
    - Wakes the Render server if sleeping
    - Checks today's meal (PKT date)
    - Sends surveys to availed users
    - Skips holidays silently
    - Returns minimal JSON response
    """
    # Verify secret
    cron_secret = os.environ.get("CRON_SECRET", "")
    provided = request.args.get("secret", "") or (request.get_json(silent=True) or {}).get("secret", "")
    if cron_secret and provided != cron_secret:
        return jsonify({"error": "Invalid cron secret"}), 403

    today_pkt = _pkt_today()
    now_str = _pkt_now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[cron] === Daily survey triggered at {now_str} PKT for {today_pkt} ===")

    # Check if meal exists for today
    meal_entry = db.get_meal_by_date(today_pkt)
    if not meal_entry:
        log_entry = {
            "timestamp": now_str,
            "date": today_pkt,
            "status": "skipped",
            "reason": "holiday",
            "message": f"No meal for {today_pkt} — holiday",
            "sent": 0, "skipped": 0, "errors": 0,
        }
        _auto_send_log.append(log_entry)
        if len(_auto_send_log) > MAX_AUTO_LOG:
            _auto_send_log.pop(0)
        print(f"[cron] Holiday — no meal for {today_pkt}")
        return jsonify(log_entry)

    meal_name = meal_entry.get("meal_name", "")
    if not meal_name:
        return jsonify({"status": "error", "message": "Meal exists but name empty"}), 500

    # Get availed users
    available_users = db.get_available_users_for_date(today_pkt)
    if not available_users:
        log_entry = {
            "timestamp": now_str,
            "date": today_pkt,
            "status": "skipped",
            "reason": "no_availed_users",
            "message": f"No users availed for {today_pkt}",
            "mealName": meal_name,
            "sent": 0, "skipped": 0, "errors": 0,
        }
        _auto_send_log.append(log_entry)
        if len(_auto_send_log) > MAX_AUTO_LOG:
            _auto_send_log.pop(0)
        print(f"[cron] No availed users for {today_pkt}")
        return jsonify(log_entry)

    # Send surveys
    sent_count = 0
    skip_count = 0
    error_count = 0

    for user in available_users:
        phone = user["phoneno"]
        user_id = user["id"]
        user_name = user.get("name", "")

        response = db.create_response(user_id, today_pkt, meal_name)
        if not response:
            skip_count += 1
            continue

        try:
            wa.send_survey(phone, meal_name, "button")
            sent_count += 1
        except Exception as e:
            print(f"[cron] Failed to send to {phone}: {e}")
            error_count += 1

    log_entry = {
        "timestamp": now_str,
        "date": today_pkt,
        "status": "completed",
        "mealName": meal_name,
        "sent": sent_count,
        "skipped": skip_count,
        "errors": error_count,
        "totalAvailable": len(available_users),
    }
    _auto_send_log.append(log_entry)
    if len(_auto_send_log) > MAX_AUTO_LOG:
        _auto_send_log.pop(0)

    print(f"[cron] Done: {sent_count} sent, {skip_count} skipped, {error_count} errors")
    return jsonify(log_entry)


# ── Scheduler Status API (lightweight — just reads in-memory log) ────


@app.route("/api/scheduler/status")
def scheduler_status():
    """Get auto-send status. Lightweight — just reads in-memory log."""
    last_log = _auto_send_log[-1] if _auto_send_log else None
    return jsonify({
        "method": "external_cron",
        "schedule": "2:30 PM PKT daily via cron-job.org",
        "cronEndpoint": "/api/cron/daily-survey?secret=YOUR_CRON_SECRET",
        "lastRun": last_log,
    })


@app.route("/api/scheduler/logs")
def scheduler_logs():
    """Get recent auto-send logs."""
    return jsonify({"logs": _auto_send_log, "count": len(_auto_send_log)})


# ── User Management API ──────────────────────────────────────────────


@app.route("/api/users", methods=["GET"])
def get_users():
    try:
        users = db.get_all_users()
        return jsonify(users)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users", methods=["POST"])
def add_user():
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
    data = request.get_json(silent=True) or {}
    try:
        user = db.update_user(user_id, **data)
        return jsonify(user)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<user_id>", methods=["DELETE"])
def delete_user(user_id):
    try:
        db.delete_user(user_id)
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/available", methods=["GET"])
def get_available_users():
    try:
        users = db.get_available_users()
        return jsonify(users)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mealavail/<meal_date>", methods=["GET"])
def get_mealavail(meal_date):
    try:
        entries = db.get_mealavail_for_date(meal_date)
        return jsonify(entries)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mealavail", methods=["POST"])
def set_mealavail():
    data = request.get_json(silent=True) or {}
    meal_date = data.get("mealDate", "").strip()
    user_id = data.get("userId", "").strip()
    availstatus = data.get("availstatus", False)

    if not meal_date or not user_id:
        return jsonify({"error": "mealDate and userId are required."}), 400

    try:
        result = db.set_mealavail(meal_date, user_id, availstatus)
        return jsonify(result or {"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mealavail/bulk", methods=["POST"])
def set_mealavail_bulk():
    data = request.get_json(silent=True) or {}
    meal_date = data.get("mealDate", "").strip()
    user_ids = data.get("userIds", [])
    availstatus = data.get("availstatus", True)

    if not meal_date or not user_ids:
        return jsonify({"error": "mealDate and userIds are required."}), 400

    try:
        count = db.set_mealavail_bulk(meal_date, user_ids, availstatus)
        return jsonify({"status": "updated", "count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/meal/<meal_date>", methods=["GET"])
def get_meal_by_date(meal_date):
    try:
        meal = db.get_meal_by_date(meal_date)
        if meal:
            return jsonify(meal)
        return jsonify({"meal_name": None}), 200
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
    try:
        keys = db.get_all_state_keys()
        return jsonify({"activePhones": keys, "count": len(keys)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Run ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[app] Starting WhatsApp Meal Survey on port {port}")
    print(f"[app] Cron endpoint: /api/cron/daily-survey?secret=CRON_SECRET")
    app.run(host="0.0.0.0", port=port, debug=True)
