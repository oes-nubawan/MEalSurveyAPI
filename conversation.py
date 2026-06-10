"""
Conversation handler — processes incoming WhatsApp messages with a state machine.

Duplicate prevention: ONE response per phone per surveyDate.
- If user already has complete feedback for a date, block with "already submitted"
- If survey_date is lost (worker restart), recover from database
- A new survey for a different date allows a new response
- Same date = "already submitted feedback for today"

Not Acceptable flow:
- User selects "Not Acceptable" → partial entry saved → WAITING_COMMENT state
- Next text message from user is captured as their complaint
- SAFETY NET: If WAITING_COMMENT state is lost (e.g. server restart), we also
  check for a partial not_ok entry with no comment and treat text as complaint.
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
    return f"{phone.lower()}::{survey_date}"


def is_completed_for_date(phone: str, survey_date: str) -> bool:
    with _completed_lock:
        return _make_key(phone, survey_date) in _completed


def mark_completed_for_date(phone: str, survey_date: str):
    with _completed_lock:
        _completed[_make_key(phone, survey_date)] = True


def unmark_all_for_phone(phone: str):
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

# Known rating IDs — if input matches one of these, it's a rating selection, NOT free text
RATING_IDS = frozenset({
    "very_good", "good", "satisfactory", "not_ok",
    "ts_very_good", "ts_good", "ts_satisfactory", "ts_not_ok",
    "add_comment", "skip_comment",
})


def handle(user: str, input_text: str, wa: WhatsAppService):
    """
    Main entry point — process an incoming message from a WhatsApp user.
    Uses per-phone locking to prevent race conditions from rapid taps.
    """
    if not user or not user.strip():
        print("[conversation] Empty user, ignoring")
        return

    print(f"[conversation] ===== Handle START: user={user}, input='{input_text}' =====")

    lock = _get_phone_lock(user)
    with lock:
        try:
            _handle_internal(user, input_text, wa)
        except Exception as e:
            print(f"[conversation] EXCEPTION in _handle_internal: {e}")
            import traceback
            traceback.print_exc()
    print(f"[conversation] ===== Handle END: user={user} =====")


def _is_feedback_complete(entry: dict) -> bool:
    """Check if a feedback entry is complete (no further action needed)."""
    if not entry:
        return False
    rating = entry.get("rating", "")
    comment = entry.get("comment")
    # Positive ratings are always complete (comment is None by design)
    if rating in ("very_good", "good", "satisfactory"):
        return True
    # not_ok is complete only if it has a comment (or was skipped)
    if rating == "not_ok":
        return comment is not None
    return False


def _handle_internal(user: str, input_text: str, wa: WhatsAppService):
    """Internal handler — runs under per-phone lock."""

    # ── CRITICAL: Recover from worker restart ──
    store.reload_if_empty()

    state = store.get_state(user)
    survey_date = state.get("surveyDate", "") if state else ""
    meal_name = state.get("mealName", "") if state else ""

    print(f"[conversation] State: {state}")
    print(f"[conversation] Parsed: surveyDate='{survey_date}', mealName='{meal_name}'")
    print(f"[conversation] All state keys: {store.get_all_state_keys()}")

    # ══════════════════════════════════════════════════════════════════════
    #  WAITING_COMMENT: user selected "Not Acceptable" and we're waiting
    #  for them to type their complaint.
    # ══════════════════════════════════════════════════════════════════════
    if state and state.get("state") == "WAITING_COMMENT":
        print(f"[conversation] >>> WAITING_COMMENT state detected for {user}")
        if input_text == "skip_complaint":
            _finalize_not_acceptable(user, survey_date, None, "skipped", wa)
        else:
            _finalize_not_acceptable(user, survey_date, input_text, "free_text", wa)
        return

    # ══════════════════════════════════════════════════════════════════════
    #  WAITING_TWO_STEP_COMMENT: two-step flow, user chose "Add comment"
    # ══════════════════════════════════════════════════════════════════════
    if state and state.get("state") == "WAITING_TWO_STEP_COMMENT":
        print(f"[conversation] >>> WAITING_TWO_STEP_COMMENT state detected for {user}")
        if input_text.lower() == "skip" or input_text == "skip_comment":
            mark_completed_for_date(user, survey_date)
            store.remove_state(user)
            wa.send_text(user, "Thank you for your feedback!")
        else:
            _handle_two_step_comment(user, survey_date, input_text, wa)
        return

    # ══════════════════════════════════════════════════════════════════════
    #  SAFETY NET: State was lost but DB has partial not_ok entry.
    #  User types free text → we catch it as their complaint.
    # ══════════════════════════════════════════════════════════════════════
    if input_text not in RATING_IDS:
        # Check with survey date if we have it
        if survey_date:
            incomplete = store.has_incomplete_not_ok(user, survey_date)
            if incomplete:
                print(f"[conversation] >>> SAFETY NET (with date): Found incomplete not_ok for {user} on {survey_date}. Text='{input_text}' treated as complaint.")
                _finalize_not_acceptable(user, survey_date, input_text, "free_text", wa)
                return

        # Check without date (state was completely lost)
        any_incomplete = store.has_any_incomplete_not_ok(user)
        if any_incomplete:
            found_date = any_incomplete.get("surveyDate", "")
            found_meal = any_incomplete.get("mealName", "")
            if found_date:
                print(f"[conversation] >>> SAFETY NET (no state, DB recovery): Found incomplete not_ok for {user} on {found_date}. Text='{input_text}' treated as complaint.")
                store.set_state(user, "WAITING_COMMENT", found_meal, found_date)
                _finalize_not_acceptable(user, found_date, input_text, "free_text", wa)
                return

    # ══════════════════════════════════════════════════════════════════════
    #  DUPLICATE CHECK — ALWAYS check DB, even without survey_date
    #  This is the most important check. One response per phone per date.
    #  If state was lost, recover the survey_date from existing entries.
    # ══════════════════════════════════════════════════════════════════════

    # Step 1: If we have a survey_date, check by date (normal path)
    if survey_date:
        if is_completed_for_date(user, survey_date):
            print(f"[conversation] DUPLICATE BLOCK (in-memory): User {user} already completed for {survey_date}.")
            wa.send_text(user, "You have already submitted your feedback for today. Thank you!")
            return

        existing = store.get_latest_by_phone_and_date(user, survey_date)
        if existing:
            if _is_feedback_complete(existing):
                print(f"[conversation] DUPLICATE BLOCK (DB with date): User {user} has complete feedback for {survey_date}.")
                mark_completed_for_date(user, survey_date)
                wa.send_text(user, "You have already submitted your feedback for today. Thank you!")
                return
            # not_ok with no comment but not in WAITING_COMMENT — re-prompt
            print(f"[conversation] User {user} has incomplete not_ok for {survey_date}. Re-sending prompt.")
            store.set_state(user, "WAITING_COMMENT", existing.get("mealName", ""), survey_date)
            wa.send_text(user, f"You selected Not Acceptable for {existing.get('mealName', 'the meal')}. Please type your feedback about what went wrong:")
            return

    # Step 2: NO survey_date (state was lost) — check ALL existing feedback
    # If user has ANY complete feedback entry, they already responded.
    # We recover the survey_date from the latest entry.
    else:
        latest = store.get_latest_by_phone(user)
        if latest:
            recovered_date = latest.get("surveyDate", "")
            if _is_feedback_complete(latest):
                print(f"[conversation] DUPLICATE BLOCK (DB no state): User {user} has complete feedback (date={recovered_date}). Blocking.")
                if recovered_date:
                    mark_completed_for_date(user, recovered_date)
                wa.send_text(user, "You have already submitted your feedback for today. Thank you!")
                return
            # Has incomplete not_ok — recover the date and re-prompt
            if latest.get("rating") == "not_ok" and latest.get("comment") is None:
                print(f"[conversation] RECOVERY: User {user} has incomplete not_ok (date={recovered_date}). Recovering state and re-prompting.")
                if recovered_date:
                    store.set_state(user, "WAITING_COMMENT", latest.get("mealName", ""), recovered_date)
                    wa.send_text(user, f"You selected Not Acceptable for {latest.get('mealName', 'the meal')}. Please type your feedback about what went wrong:")
                else:
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
        rating = input_text[3:]
        _handle_two_step_rating(user, rating, meal_name, survey_date, wa)
        return

    # Standard positive ratings
    if input_text in ("very_good", "good", "satisfactory"):
        _handle_positive_rating(user, input_text, meal_name, survey_date, wa)
        return

    # Two-step add_comment / skip_comment
    if input_text == "add_comment":
        store.set_state(user, "WAITING_TWO_STEP_COMMENT", meal_name, survey_date)
        wa.send_text(user, "Please type your comment below:")
        return

    if input_text == "skip_comment":
        mark_completed_for_date(user, survey_date)
        store.remove_state(user)
        wa.send_text(user, "Thank you for your feedback!")
        return

    # ══════════════════════════════════════════════════════════════════════
    #  LAST RESORT: Unrecognised input — check DB for any incomplete entry
    # ══════════════════════════════════════════════════════════════════════
    if input_text not in RATING_IDS:
        any_incomplete = store.has_any_incomplete_not_ok(user)
        if any_incomplete:
            found_date = any_incomplete.get("surveyDate", "")
            found_meal = any_incomplete.get("mealName", "")
            if found_date:
                print(f"[conversation] >>> LAST RESORT DB RECOVERY: Found incomplete not_ok for {user} on {found_date}. Treating '{input_text}' as complaint.")
                store.set_state(user, "WAITING_COMMENT", found_meal, found_date)
                _finalize_not_acceptable(user, found_date, input_text, "free_text", wa)
                return

    print(f"[conversation] Unrecognised input from {user}: '{input_text}'")
    wa.send_text(user, "Please choose a rating from the options provided.")


# ═════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ═════════════════════════════════════════════════════════════════════════


def _handle_positive_rating(user: str, rating: str, meal_name: str, survey_date: str, wa: WhatsAppService):
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
    print(f"[conversation] User {user} rated Not Acceptable for date: {survey_date}")

    # Check if there's already a partial not_ok entry for this date
    existing = store.get_latest_by_phone_and_date(user, survey_date) if survey_date else None
    if existing and existing.get("rating") == "not_ok" and existing.get("comment") is None:
        # Already have a partial entry — just re-set state and re-prompt
        print(f"[conversation] Partial not_ok already exists for {user} on {survey_date}. Re-prompting.")
    else:
        # Save a new partial entry
        entry = {
            "phone": user,
            "mealName": meal_name or "the meal",
            "rating": "not_ok",
            "comment": None,
            "commentCategory": None,
            "surveyDate": survey_date,
        }
        store.add_feedback(entry)

    # Set state to WAITING_COMMENT
    store.set_state(user, "WAITING_COMMENT", meal_name or "the meal", survey_date)

    try:
        wa.send_text(
            user,
            f"You selected Not Acceptable for {meal_name or 'the meal'}. "
            "Please type your feedback about what went wrong:"
        )
    except Exception as e:
        print(f"[conversation] Failed to send Not Acceptable prompt to {user}: {e}")


def _finalize_not_acceptable(user: str, survey_date: str, complaint_text: Optional[str], category: str, wa: WhatsAppService):
    print(f"[conversation] Finalizing Not Acceptable for {user} on {survey_date}. Complaint: {complaint_text or '(skipped)'}")

    store.update_feedback_comment(user, complaint_text, category)
    mark_completed_for_date(user, survey_date)
    store.remove_state(user)

    if complaint_text:
        wa.send_text(user, "Thank you for your detailed feedback!")
    else:
        wa.send_text(user, "Thank you for your feedback!")


def _handle_two_step_rating(user: str, rating: str, meal_name: str, survey_date: str, wa: WhatsAppService):
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
    """
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
                print(f"[conversation] Interactive but no list_reply/button_reply: {interactive}")

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
                print(f"[conversation] Template button text as input: {input_val}")

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
