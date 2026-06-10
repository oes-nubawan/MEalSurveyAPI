"""
Conversation handler — processes incoming WhatsApp messages.

STATE IS IN THE DATABASE. No in-memory state needed.
The response_status field in tbl_usersresponse IS the state machine:
  pending  → survey sent, waiting for rating
  rated    → rating given (not_ok), waiting for remarks
  completed → fully done (positive rating OR not_ok + remarks)

Duplicate prevention: ONE row per user per meal_date (UNIQUE constraint).
Once a user gives a rating, they CANNOT change it. Block with "already submitted".
Any extra text after completing is saved as remarks.
"""

import threading
from typing import Optional
from whatsapp_service import WhatsAppService
from store import store as db


# ── Per-phone locks to serialize concurrent webhook calls ───────────────
_phone_locks: dict[str, threading.Lock] = {}
_phone_locks_lock = threading.Lock()


def _get_phone_lock(phone: str) -> threading.Lock:
    with _phone_locks_lock:
        if phone not in _phone_locks:
            _phone_locks[phone] = threading.Lock()
        return _phone_locks[phone]


# ── Constants ──────────────────────────────────────────────────────────

TEMPLATE_BUTTON_MAP = {
    "Very Good": "very_good",
    "Good": "good",
    "Satisfactory": "satisfactory",
    "Not Acceptable": "not_ok",
    "Skip": "skip_complaint",
    "No comment": "skip_comment",
    "Add comment": "add_comment",
}

RATING_DISPLAY = {
    "very_good": "Very Good",
    "good": "Good",
    "satisfactory": "Satisfactory",
    "not_ok": "Not Acceptable",
}

# Known rating IDs — anything matching these is a rating button tap, not free text
RATING_IDS = frozenset({
    "very_good", "good", "satisfactory", "not_ok",
    "ts_very_good", "ts_good", "ts_satisfactory", "ts_not_ok",
    "add_comment", "skip_comment",
})


def handle(user: str, input_text: str, wa: WhatsAppService):
    """Main entry point — process an incoming WhatsApp message."""
    if not user or not user.strip():
        print("[conversation] Empty user, ignoring")
        return

    print(f"[conversation] ===== Handle START: user={user}, input='{input_text}' =====")

    lock = _get_phone_lock(user)
    with lock:
        try:
            _handle_internal(user, input_text, wa)
        except Exception as e:
            print(f"[conversation] EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
    print(f"[conversation] ===== Handle END: user={user} =====")


def _handle_internal(user_phone: str, input_text: str, wa: WhatsAppService):
    """Core logic — DB is the state machine."""

    # ── 1. Find the user ────────────────────────────────────────────
    user = db.get_user_by_phone(user_phone)
    if not user:
        print(f"[conversation] Unknown phone: {user_phone}")
        wa.send_text(user_phone, "You are not registered for meal surveys. Please contact admin.")
        return

    user_id = user["id"]
    user_name = user.get("name", "")
    print(f"[conversation] User: {user_name} ({user_phone}), id={user_id}")

    # ── 2. Find active response ─────────────────────────────────────
    active = db.get_active_response(user_id)

    if not active:
        # No pending/rated response — check if they already completed one
        latest = db.get_latest_response_by_phone(user_phone)
        if latest and latest.get("response_status") == "completed":
            # Already completed — save any extra text as remarks if they had none
            if latest.get("remarks") is None and input_text not in RATING_IDS and latest.get("user_response") != "not_ok":
                print(f"[conversation] Saving extra comment from {user_name}: '{input_text}'")
                db.update_response(user_id, latest["meal_date"], remarks=input_text)
            print(f"[conversation] User {user_name} already completed. Blocking.")
            wa.send_text(user_phone, "You have already submitted your feedback. Thank you!")
            return
        # No survey sent to this user at all
        print(f"[conversation] No active survey for {user_name}")
        wa.send_text(user_phone, "No active survey found. Please wait for a survey to be sent.")
        return

    meal_date = active["meal_date"]
    meal_name = active["meal_name"]
    status = active.get("response_status", "pending")
    existing_rating = active.get("user_response")

    print(f"[conversation] Active response: date={meal_date}, meal={meal_name}, status={status}, rating={existing_rating}")

    # ── 3. If already completed → block, but save extra text ────────
    if status == "completed":
        # Save any extra text as remarks (for positive ratings, remarks are optional extras)
        if existing_rating != "not_ok" and active.get("remarks") is None and input_text not in RATING_IDS:
            print(f"[conversation] Saving extra comment from {user_name}: '{input_text}'")
            db.update_response(user_id, meal_date, remarks=input_text)
        print(f"[conversation] User {user_name} already completed for {meal_date}. Blocking.")
        wa.send_text(user_phone, "You have already submitted your feedback. Thank you!")
        return

    # ── 4. If rated not_ok → waiting for remarks ────────────────────
    if status == "rated" and existing_rating == "not_ok":
        if input_text in RATING_IDS:
            # User tapped another button instead of typing — BLOCK, don't re-prompt
            print(f"[conversation] User {user_name} already rated Not Acceptable. Blocking duplicate rating tap.")
            wa.send_text(user_phone, "You have already submitted your feedback. Thank you!")
            return
        # Save remarks and complete
        print(f"[conversation] Saving remarks for {user_name}: '{input_text}'")
        db.update_response(user_id, meal_date,
            remarks=input_text,
            response_status="completed",
        )
        wa.send_text(user_phone, "Thank you for your feedback!")
        return

    # ── 5. If pending → waiting for rating ──────────────────────────
    if status == "pending":
        # Handle standard ratings
        if input_text in ("very_good", "good", "satisfactory", "not_ok"):
            _process_rating(user_phone, user_id, meal_date, meal_name, input_text, wa)
            return

        # Handle two-step ratings
        if input_text.startswith("ts_"):
            rating = input_text[3:]
            _process_rating(user_phone, user_id, meal_date, meal_name, rating, wa)
            return

        # Template buttons
        if input_text in ("add_comment", "skip_comment"):
            wa.send_text(user_phone, "Please choose a rating from the options provided.")
            return

        # Free text when pending — ask for rating
        print(f"[conversation] Unrecognized input from {user_name} (pending): '{input_text}'")
        wa.send_text(user_phone, "Please choose a rating from the options provided.")
        return

    # ── 6. Unknown state ────────────────────────────────────────────
    print(f"[conversation] Unknown state '{status}' for {user_name}")
    wa.send_text(user_phone, "Please choose a rating from the options provided.")


def _process_rating(user_phone: str, user_id: str, meal_date: str, meal_name: str, rating: str, wa: WhatsAppService):
    """Process a rating selection."""
    print(f"[conversation] User rated: {rating} for {meal_name} on {meal_date}")

    if rating == "not_ok":
        # Save rating, set status to 'rated' (waiting for remarks)
        db.update_response(user_id, meal_date,
            user_response="not_ok",
            response_status="rated",
        )
        wa.send_text(user_phone, f"You selected Not Acceptable for {meal_name}. Please type your feedback:")
    else:
        # Positive rating — complete immediately
        db.update_response(user_id, meal_date,
            user_response=rating,
            response_status="completed",
        )
        wa.send_text(user_phone, "Thank you for your feedback!")


# ── Message parsing ───────────────────────────────────────────────────


def parse_webhook_message(message: dict) -> tuple[str, str]:
    """Parse a WhatsApp message dict and extract (from, input)."""
    from_number = message.get("from", "")
    msg_type = message.get("type", "")
    input_val = ""

    print(f"[conversation] Parsing message from={from_number}, type={msg_type}")

    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        list_reply = interactive.get("list_reply")
        if list_reply and "id" in list_reply:
            input_val = list_reply["id"]
            print(f"[conversation] List reply id: {input_val}")
        else:
            button_reply = interactive.get("button_reply")
            if button_reply and "id" in button_reply:
                input_val = button_reply["id"]
                print(f"[conversation] Button reply id: {input_val}")
            else:
                print(f"[conversation] Interactive but no reply found: {interactive}")

    elif msg_type == "button":
        button_obj = message.get("button", {})
        input_val = button_obj.get("payload", "")
        if input_val:
            print(f"[conversation] Template button payload: {input_val}")
        else:
            button_text = button_obj.get("text", "")
            mapped = TEMPLATE_BUTTON_MAP.get(button_text)
            if mapped:
                input_val = mapped
                print(f"[conversation] Mapped template button '{button_text}' to {input_val}")
            else:
                input_val = button_text
                print(f"[conversation] Template button text: {input_val}")

    elif msg_type == "text":
        text_obj = message.get("text", {})
        input_val = text_obj.get("body", "")
        print(f"[conversation] Text body: '{input_val}'")

    else:
        print(f"[conversation] Ignoring message type: {msg_type}")

    return from_number, input_val


def process_webhook(body: dict, wa: WhatsAppService):
    """Process a full webhook payload body."""
    print(f"[conversation] Webhook received")

    entries = body.get("entry", [])
    if not entries:
        print("[conversation] No 'entry' in webhook payload")
        return

    for entry_item in entries:
        changes = entry_item.get("changes", [])
        for change in changes:
            value = change.get("value", {})

            if "statuses" in value:
                print("[conversation] Skipping status update webhook")
                continue

            messages = value.get("messages", [])
            if not messages:
                continue

            for message in messages:
                try:
                    from_number, input_text = parse_webhook_message(message)
                    if not from_number:
                        print("[conversation] Message missing 'from' field")
                        continue
                    if not input_text:
                        print(f"[conversation] Empty input from {from_number}, skipping")
                        continue

                    print(f"[conversation] Parsed: from={from_number}, input='{input_text}'")
                    handle(from_number, input_text, wa)
                except Exception as e:
                    print(f"[conversation] Error processing message: {e}")
                    import traceback
                    traceback.print_exc()
