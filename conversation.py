"""
Conversation handler — processes incoming WhatsApp messages with a state machine.

Key design decisions (fixing the .NET bugs):
1. DUPLICATE PREVENTION: A completed_phones set is checked FIRST, before any
   processing. Once a phone has submitted a complete response, ALL subsequent
   messages from that phone are rejected.
2. NOT ACCEPTABLE FLOW: When user selects "Not Acceptable", we save a partial
   entry (rating=not_ok, comment=None), set state to WAITING_COMMENT, and send
   a plain text message asking for their feedback. The NEXT text message the
   user sends is captured as the complaint.
3. STATE PERSISTENCE: State is saved to JSON so it survives restarts.
"""

import threading
from typing import Optional
from whatsapp_service import WhatsAppService
import store


# ── Per-phone locks to serialize concurrent webhook calls ───────────────
_phone_locks: dict[str, threading.Lock] = {}
_phone_locks_lock = threading.Lock()

# ── Completed phones set — checked FIRST to block duplicates ───────────
# Maps phone -> timestamp of completion
_completed_phones: dict[str, float] = {}
_completed_lock = threading.Lock()


def _get_phone_lock(phone: str) -> threading.Lock:
    with _phone_locks_lock:
        if phone not in _phone_locks:
            _phone_locks[phone] = threading.Lock()
        return _phone_locks[phone]


def is_phone_completed(phone: str) -> bool:
    """Check if a phone has already submitted feedback."""
    with _completed_lock:
        return phone.lower() in _completed_phones


def mark_phone_completed(phone: str):
    """Mark a phone as having completed feedback."""
    with _completed_lock:
        _completed_phones[phone.lower()] = True


def unmark_phone_completed(phone: str):
    """Remove phone from completed set (for testing/reset)."""
    with _completed_lock:
        _completed_phones.pop(phone.lower(), None)


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
    print(f"[conversation] User state: {state}")

    # ══════════════════════════════════════════════════════════════════════
    #  WAITING_COMMENT: user selected "Not Acceptable" and we're waiting
    #  for them to type their complaint. The NEXT text message they send
    #  is their complaint.
    # ══════════════════════════════════════════════════════════════════════
    if state and state.get("state") == "WAITING_COMMENT":
        if input_text == "skip_complaint":
            _finalize_not_acceptable(user, None, "skipped", wa)
        else:
            # Any text input is the user's complaint
            _finalize_not_acceptable(user, input_text, "free_text", wa)
        return

    # ══════════════════════════════════════════════════════════════════════
    #  WAITING_TWO_STEP_COMMENT: two-step flow, user chose "Add comment"
    # ══════════════════════════════════════════════════════════════════════
    if state and state.get("state") == "WAITING_TWO_STEP_COMMENT":
        if input_text.lower() == "skip" or input_text == "skip_comment":
            print(f"[conversation] User {user} skipped comment (two-step)")
            mark_phone_completed(user)
            store.remove_state(user)
            wa.send_text(user, "Thank you for your feedback! You cannot respond again.")
        else:
            _handle_two_step_comment(user, input_text, wa)
        return

    # ══════════════════════════════════════════════════════════════════════
    #  DUPLICATE RESPONSE GUARD — block if user already responded
    #  This is checked AFTER waiting states but BEFORE any new processing
    # ══════════════════════════════════════════════════════════════════════
    if is_phone_completed(user):
        print(f"[conversation] User {user} already completed feedback (in-memory). Ignoring.")
        wa.send_text(user, "You have already submitted your feedback. Thank you!")
        return

    existing = store.get_latest_by_phone(user)
    if existing:
        is_complete = existing.get("rating") != "not_ok" or existing.get("comment") is not None
        if is_complete:
            print(f"[conversation] User {user} already submitted feedback (DB). Ignoring duplicate.")
            mark_phone_completed(user)
            wa.send_text(user, "You have already submitted your feedback. Thank you!")
            return
        # If not_ok with no comment, user is in WAITING_COMMENT flow but state was lost
        print(f"[conversation] User {user} has incomplete not_ok entry. Re-sending prompt.")
        store.set_state(user, "WAITING_COMMENT", existing.get("mealName", ""))
        wa.send_text(user, f"You selected Not Acceptable for {existing.get('mealName', 'the meal')}. Please type your feedback about what went wrong:")
        return

    # ══════════════════════════════════════════════════════════════════════
    #  ROUTE INPUT TO HANDLERS
    # ══════════════════════════════════════════════════════════════════════

    meal_name = state.get("mealName", "") if state else ""

    # "Not Acceptable" from ANY survey type
    if input_text in ("not_ok", "ts_not_ok"):
        _handle_not_acceptable(user, meal_name, wa)
        return

    # Two-step positive rating (ts_ prefix)
    if input_text.startswith("ts_"):
        rating = input_text[3:]  # strip "ts_" prefix
        _handle_two_step_rating(user, rating, meal_name, wa)
        return

    # Standard positive ratings
    if input_text in ("very_good", "good", "satisfactory"):
        _handle_positive_rating(user, input_text, meal_name, wa)
        return

    # Two-step add_comment / skip_comment (when not in WAITING state)
    if input_text == "add_comment":
        print(f"[conversation] User {user} wants to add comment (two-step).")
        store.set_state(user, "WAITING_TWO_STEP_COMMENT", meal_name)
        wa.send_text(user, "Please type your comment below:")
        return

    if input_text == "skip_comment":
        print(f"[conversation] User {user} skipped comment (two-step).")
        mark_phone_completed(user)
        store.remove_state(user)
        wa.send_text(user, "Thank you for your feedback! You cannot respond again.")
        return

    # Unrecognised input
    print(f"[conversation] User {user} sent unknown input: {input_text}")
    wa.send_text(user, "Please choose a rating from the options provided.")


# ═════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ═════════════════════════════════════════════════════════════════════════


def _handle_positive_rating(user: str, rating: str, meal_name: str, wa: WhatsAppService):
    """Positive/neutral rating — save immediately, one-and-done."""
    print(f"[conversation] User {user} rated: {rating}")

    entry = {
        "phone": user,
        "mealName": meal_name,
        "rating": rating,
        "comment": None,
        "commentCategory": None,
    }
    store.add_feedback(entry)
    mark_phone_completed(user)
    store.remove_state(user)
    wa.send_text(user, "Thank you for your feedback! You cannot respond again.")


def _handle_not_acceptable(user: str, meal_name: str, wa: WhatsAppService):
    """
    "Not Acceptable" selected — save partial entry, set WAITING_COMMENT state,
    send a plain text message asking user to type their complaint.
    """
    print(f"[conversation] User {user} rated Not Acceptable. Asking for free-text complaint.")

    # Save a partial entry — marks the user as having responded
    entry = {
        "phone": user,
        "mealName": meal_name or "the meal",
        "rating": "not_ok",
        "comment": None,
        "commentCategory": None,
    }
    store.add_feedback(entry)

    # Set state so we capture the next text message as the complaint
    store.set_state(user, "WAITING_COMMENT", meal_name or "the meal")

    # Send plain text asking for feedback — this is more reliable than
    # interactive buttons and makes it clear to the user they should TYPE
    try:
        wa.send_text(
            user,
            f"You selected Not Acceptable for {meal_name or 'the meal'}. "
            "Please type your feedback about what went wrong:"
        )
    except Exception as e:
        print(f"[conversation] Failed to send Not Acceptable prompt to {user}: {e}")
        # State is already set, so when user types something it will be captured


def _finalize_not_acceptable(user: str, complaint_text: Optional[str], category: str, wa: WhatsAppService):
    """Finalize a Not Acceptable response — update the partial entry with the complaint text."""
    print(f"[conversation] User {user} finalizing Not Acceptable. Complaint: {complaint_text or '(skipped)'}")

    store.update_feedback_comment(user, complaint_text, category)
    mark_phone_completed(user)
    store.remove_state(user)

    if complaint_text:
        wa.send_text(user, "Thank you for your detailed feedback! You cannot respond again.")
    else:
        wa.send_text(user, "Thank you for your feedback! You cannot respond again.")


def _handle_two_step_rating(user: str, rating: str, meal_name: str, wa: WhatsAppService):
    """Two-step survey positive rating — saves rating, then asks for optional comment."""
    print(f"[conversation] User {user} rated (two-step): {rating}")

    entry = {
        "phone": user,
        "mealName": meal_name,
        "rating": rating,
        "comment": None,
        "commentCategory": None,
    }
    store.add_feedback(entry)
    store.set_state(user, "WAITING_TWO_STEP_COMMENT", meal_name)

    display = RATING_DISPLAY.get(rating, rating)
    try:
        wa.send_text(
            user,
            f"You rated {meal_name or 'the meal'} as {display}. "
            "Would you like to add a comment? Type your comment, or type 'skip' to skip."
        )
    except Exception as e:
        print(f"[conversation] Failed to send comment prompt to {user}: {e}")


def _handle_two_step_comment(user: str, comment: str, wa: WhatsAppService):
    """Two-step: save the free-text comment."""
    print(f"[conversation] User {user} adding optional comment: {comment}")

    store.update_feedback_comment(user, comment, None)
    mark_phone_completed(user)
    store.remove_state(user)
    wa.send_text(user, "Thank you for your feedback! You cannot respond again.")


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
