"""
Conversation handler — processes incoming WhatsApp messages with a state machine.

Duplicate prevention: DATE-BASED. One response per phone per surveyDate.
- When a new survey is sent, the survey date is stored in the user's state.
- The duplicate check looks for existing feedback for that phone + surveyDate.
- A new survey for a different date allows the user to respond again.

Not Acceptable flow:
- User selects "Not Acceptable" → partial entry saved → WAITING_COMMENT state
- Next text message from user is captured as their complaint
- State persisted to JSON so it survives restarts
"""

import threading
from typing import Optional
from whatsapp_service import WhatsAppService
import store


# ── Per-phone locks to serialize concurrent webhook calls ───────────────
_phone_locks: dict[str, threading.Lock] = {}
_phone_locks_lock = threading.Lock()

# ── Completed phones+date set — fast in-memory duplicate check ─────────
# Key format: "phone_lower::YYYY-MM-DD" → True
_completed: dict[str, bool] = {}
_completed_lock = threading.Lock()


def _get_phone_lock(phone: str) -> threading.Lock:
    with _phone_locks_lock:
        if phone not in _phone_locks:
            _phone_locks[phone] = threading.Lock()
        return _phone_locks[phone]


def _make_key(phone: str, survey_date: str) -> str:
    """Create a composite key for the completed set: phone::date"""
    return f"{phone.lower()}::{survey_date}"


def is_completed_for_date(phone: str, survey_date: str) -> bool:
    """Check if a phone has already submitted feedback for a specific date."""
    with _completed_lock:
        return _make_key(phone, survey_date) in _completed


def mark_completed_for_date(phone: str, survey_date: str):
    """Mark a phone as having completed feedback for a specific date."""
    with _completed_lock:
        _completed[_make_key(phone, survey_date)] = True


def unmark_completed_for_date(phone: str, survey_date: str):
    """Remove phone+date from completed set (for testing/reset)."""
    with _completed_lock:
        _completed.pop(_make_key(phone, survey_date), None)


def unmark_all_for_phone(phone: str):
    """Remove all date entries for a phone (for testing/reset)."""
    with _completed_lock:
        prefix = phone.lower() + "::"
        keys_to_remove = [k for k in _completed if k.startswith(prefix)]
        for k in keys_to_remove:
            del _completed[k]


# ── Template button text → internal rating ID mapping ──────────────────
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


def handle(user: str, input_text: str, wa: WhatsAppService):
    """
    Main entry point — process an incoming message from a WhatsApp user.
    Uses per-phone locking to prevent race conditions from rapid taps.
    """
    if not user or not user.strip():
        print("[conversation] Empty user, ignoring")
        return

    print(f"[conversation] Handle: user={user}, input={input_text}")

    lock = _get_phone_lock(user)
    with lock:
        _handle_internal(user, input_text, wa)


def _handle_internal(user: str, input_text: str, wa: WhatsAppService):
    """Internal handler — runs under per-phone lock."""

    state = store.get_state(user)
    survey_date = state.get("surveyDate", "") if state else ""
    meal_name = state.get("mealName", "") if state else ""
    print(f"[conversation] User state: {state}")

    # ══════════════════════════════════════════════════════════════════════
    #  WAITING_COMMENT: user selected "Not Acceptable" and we're waiting
    #  for them to type their complaint.
    # ══════════════════════════════════════════════════════════════════════
    if state and state.get("state") == "WAITING_COMMENT":
        if input_text == "skip_complaint":
            _finalize_not_acceptable(user, survey_date, None, "skipped", wa)
        else:
            # Any text input is the user's complaint
            _finalize_not_acceptable(user, survey_date, input_text, "free_text", wa)
        return

    # ══════════════════════════════════════════════════════════════════════
    #  WAITING_TWO_STEP_COMMENT: two-step flow, user chose "Add comment"
    # ══════════════════════════════════════════════════════════════════════
    if state and state.get("state") == "WAITING_TWO_STEP_COMMENT":
        if input_text.lower() == "skip" or input_text == "skip_comment":
            print(f"[conversation] User {user} skipped comment (two-step)")
            mark_completed_for_date(user, survey_date)
            store.remove_state(user)
            wa.send_text(user, "Thank you for your feedback!")
        else:
            _handle_two_step_comment(user, survey_date, input_text, wa)
        return

    # ══════════════════════════════════════════════════════════════════════
    #  DATE-BASED DUPLICATE CHECK
    #  Block if user already responded for this survey date.
    #  A new survey for a different date will have a different surveyDate.
    # ══════════════════════════════════════════════════════════════════════
    if survey_date:
        if is_completed_for_date(user, survey_date):
            print(f"[conversation] User {user} already completed feedback for {survey_date}. Ignoring.")
            wa.send_text(user, "You have already submitted your feedback for today. Thank you!")
            return

        existing = store.get_latest_by_phone_and_date(user, survey_date)
        if existing:
            is_complete = existing.get("rating") != "not_ok" or existing.get("comment") is not None
            if is_complete:
                print(f"[conversation] User {user} already submitted feedback for {survey_date} (DB). Blocking duplicate.")
                mark_completed_for_date(user, survey_date)
                wa.send_text(user, "You have already submitted your feedback for today. Thank you!")
                return
            # not_ok with no comment — user is in WAITING_COMMENT flow but state was lost
            print(f"[conversation] User {user} has incomplete not_ok entry for {survey_date}. Re-sending prompt.")
            store.set_state(user, "WAITING_COMMENT", existing.get("mealName", ""), survey_date)
            wa.send_text(user, f"You selected Not Acceptable for {existing.get('mealName', 'the meal')}. Please type your feedback about what went wrong:")
            return
    else:
        # No surveyDate in state — check latest feedback regardless of date
        # This handles legacy entries or messages sent without a prior survey
        existing = store.get_latest_by_phone(user)
        if existing:
            existing_date = existing.get("surveyDate", "")
            is_complete = existing.get("rating") != "not_ok" or existing.get("comment") is not None
            if is_complete:
                print(f"[conversation] User {user} has existing feedback (no surveyDate in state). Blocking.")
                wa.send_text(user, "You have already submitted your feedback. Thank you!")
                return

    # ══════════════════════════════════════════════════════════════════════
    #  ROUTE INPUT TO HANDLERS
    # ══════════════════════════════════════════════════════════════════════

    # "Not Acceptable" from ANY survey type
    if input_text in ("not_ok", "ts_not_ok"):
        _handle_not_acceptable(user, meal_name, survey_date, wa)
        return

    # Two-step positive rating (ts_ prefix)
    if input_text.startswith("ts_"):
        rating = input_text[3:]  # strip "ts_" prefix
        _handle_two_step_rating(user, rating, meal_name, survey_date, wa)
        return

    # Standard positive ratings
    if input_text in ("very_good", "good", "satisfactory"):
        _handle_positive_rating(user, input_text, meal_name, survey_date, wa)
        return

    # Two-step add_comment / skip_comment (when not in WAITING state)
    if input_text == "add_comment":
        print(f"[conversation] User {user} wants to add comment (two-step).")
        store.set_state(user, "WAITING_TWO_STEP_COMMENT", meal_name, survey_date)
        wa.send_text(user, "Please type your comment below:")
        return

    if input_text == "skip_comment":
        print(f"[conversation] User {user} skipped comment (two-step).")
        mark_completed_for_date(user, survey_date)
        store.remove_state(user)
        wa.send_text(user, "Thank you for your feedback!")
        return

    # Unrecognised input
    print(f"[conversation] User {user} sent unknown input: {input_text}")
    wa.send_text(user, "Please choose a rating from the options provided.")


# ═════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ═════════════════════════════════════════════════════════════════════════


def _handle_positive_rating(user: str, rating: str, meal_name: str, survey_date: str, wa: WhatsAppService):
    """Positive/neutral rating — save immediately, one-and-done for this date."""
    print(f"[conversation] User {user} rated: {rating} for date: {survey_date}")

    entry = {
        "phone": user,
        "mealName": meal_name,
        "rating": rating,
        "comment": None,
        "commentCategory": None,
        "surveyDate": survey_date,
    }
    store.add_feedback(entry)
    mark_completed_for_date(user, survey_date)
    store.remove_state(user)
    wa.send_text(user, "Thank you for your feedback!")


def _handle_not_acceptable(user: str, meal_name: str, survey_date: str, wa: WhatsAppService):
    """
    "Not Acceptable" selected — save partial entry, set WAITING_COMMENT state,
    send a plain text message asking user to type their complaint.
    """
    print(f"[conversation] User {user} rated Not Acceptable for date: {survey_date}. Asking for free-text complaint.")

    # Save a partial entry — marks the user as having responded for this date
    entry = {
        "phone": user,
        "mealName": meal_name or "the meal",
        "rating": "not_ok",
        "comment": None,
        "commentCategory": None,
        "surveyDate": survey_date,
    }
    store.add_feedback(entry)

    # Set state so we capture the next text message as the complaint
    store.set_state(user, "WAITING_COMMENT", meal_name or "the meal", survey_date)

    # Send plain text asking for feedback
    try:
        wa.send_text(
            user,
            f"You selected Not Acceptable for {meal_name or 'the meal'}. "
            "Please type your feedback about what went wrong:"
        )
    except Exception as e:
        print(f"[conversation] Failed to send Not Acceptable prompt to {user}: {e}")


def _finalize_not_acceptable(user: str, survey_date: str, complaint_text: Optional[str], category: str, wa: WhatsAppService):
    """Finalize a Not Acceptable response — update the partial entry with the complaint text."""
    print(f"[conversation] User {user} finalizing Not Acceptable for {survey_date}. Complaint: {complaint_text or '(skipped)'}")

    store.update_feedback_comment(user, complaint_text, category)
    mark_completed_for_date(user, survey_date)
    store.remove_state(user)

    if complaint_text:
        wa.send_text(user, "Thank you for your detailed feedback!")
    else:
        wa.send_text(user, "Thank you for your feedback!")


def _handle_two_step_rating(user: str, rating: str, meal_name: str, survey_date: str, wa: WhatsAppService):
    """Two-step survey positive rating — saves rating, then asks for optional comment."""
    print(f"[conversation] User {user} rated (two-step): {rating} for date: {survey_date}")

    entry = {
        "phone": user,
        "mealName": meal_name,
        "rating": rating,
        "comment": None,
        "commentCategory": None,
        "surveyDate": survey_date,
    }
    store.add_feedback(entry)
    store.set_state(user, "WAITING_TWO_STEP_COMMENT", meal_name, survey_date)

    display = RATING_DISPLAY.get(rating, rating)
    try:
        wa.send_text(
            user,
            f"You rated {meal_name or 'the meal'} as {display}. "
            "Would you like to add a comment? Type your comment, or type 'skip' to skip."
        )
    except Exception as e:
        print(f"[conversation] Failed to send comment prompt to {user}: {e}")


def _handle_two_step_comment(user: str, survey_date: str, comment: str, wa: WhatsAppService):
    """Two-step: save the free-text comment."""
    print(f"[conversation] User {user} adding optional comment: {comment}")

    store.update_feedback_comment(user, comment, None)
    mark_completed_for_date(user, survey_date)
    store.remove_state(user)
    wa.send_text(user, "Thank you for your feedback!")


# ── Message parsing ───────────────────────────────────────────────────


def parse_webhook_message(message: dict) -> tuple[str, str]:
    """
    Parse a WhatsApp message dict and extract (from, input).
    Handles: interactive (list_reply, button_reply), button (template quick-reply), text.
    Returns (phone_number, internal_rating_id_or_text).
    """
    from_number = message.get("from", "")
    msg_type = message.get("type", "")
    input_val = ""

    print(f"[conversation] Parsing message from={from_number}, type={msg_type}")

    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        # List reply
        list_reply = interactive.get("list_reply")
        if list_reply and "id" in list_reply:
            input_val = list_reply["id"]
            print(f"[conversation] List reply id: {input_val}")
        else:
            # Button reply (from interactive button messages)
            button_reply = interactive.get("button_reply")
            if button_reply and "id" in button_reply:
                input_val = button_reply["id"]
                print(f"[conversation] Button reply id: {input_val}")
            else:
                print(f"[conversation] Interactive message but no list_reply or button_reply: {interactive}")

    elif msg_type == "button":
        # Template quick-reply button responses
        button_obj = message.get("button", {})
        # Try payload first
        input_val = button_obj.get("payload", "")
        if input_val:
            print(f"[conversation] Template button payload: {input_val}")
        else:
            # Map button text to our IDs
            button_text = button_obj.get("text", "")
            mapped = TEMPLATE_BUTTON_MAP.get(button_text)
            if mapped:
                input_val = mapped
                print(f"[conversation] Mapped template button '{button_text}' to {input_val}")
            else:
                input_val = button_text
                print(f"[conversation] Using template button text as input: {input_val}")

    elif msg_type == "text":
        text_obj = message.get("text", {})
        input_val = text_obj.get("body", "")
        print(f"[conversation] Text body: {input_val}")

    else:
        print(f"[conversation] Ignoring message type: {msg_type}")

    return from_number, input_val


def process_webhook(body: dict, wa: WhatsAppService):
    """
    Process a full webhook payload body.
    Extracts messages and routes them through the conversation handler.
    """
    print(f"[conversation] Webhook received")

    entries = body.get("entry", [])
    if not entries:
        print("[conversation] No 'entry' in webhook payload")
        return

    for entry_item in entries:
        changes = entry_item.get("changes", [])
        for change in changes:
            value = change.get("value", {})

            # Skip status updates (delivered, read, etc.)
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
                        print(f"[conversation] Could not extract input from message: {message}")
                        continue

                    print(f"[conversation] Parsed: from={from_number}, input={input_text}")
                    handle(from_number, input_text, wa)
                except Exception as e:
                    print(f"[conversation] Error processing message: {e}")
