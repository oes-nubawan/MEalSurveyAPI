"""
JSON file-based storage for feedback entries and user states.
Thread-safe with a global lock. Data persists to data/ directory.
"""

import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback-data.json")
STATES_FILE = os.path.join(DATA_DIR, "user-states.json")

_lock = threading.Lock()

# In-memory caches (loaded from disk on startup)
_feedback_entries: list[dict] = []
_user_states: dict[str, dict] = {}  # phone -> state dict


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_feedback():
    global _feedback_entries
    try:
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, "r") as f:
                _feedback_entries = json.load(f)
    except Exception as e:
        print(f"[store] Error loading feedback: {e}")
        _feedback_entries = []


def _load_states():
    global _user_states
    try:
        if os.path.exists(STATES_FILE):
            with open(STATES_FILE, "r") as f:
                state_list = json.load(f)
                _user_states = {s["phone"]: s for s in state_list}
    except Exception as e:
        print(f"[store] Error loading states: {e}")
        _user_states = {}


def _save_feedback():
    try:
        with open(FEEDBACK_FILE, "w") as f:
            json.dump(_feedback_entries, f, indent=2, default=str)
    except Exception as e:
        print(f"[store] Error saving feedback: {e}")


def _save_states():
    try:
        with open(STATES_FILE, "w") as f:
            json.dump(list(_user_states.values()), f, indent=2, default=str)
    except Exception as e:
        print(f"[store] Error saving states: {e}")


def init():
    """Initialize storage — load data from disk."""
    _ensure_data_dir()
    _load_feedback()
    _load_states()
    print(f"[store] Loaded {len(_feedback_entries)} feedback entries, {len(_user_states)} user states")


# ── Feedback operations ────────────────────────────────────────────────

def add_feedback(entry: dict) -> dict:
    """Add a feedback entry. Auto-increments ID. Returns the entry with ID."""
    with _lock:
        next_id = max((e.get("id", 0) for e in _feedback_entries), default=0) + 1
        entry["id"] = next_id
        if "createdAt" not in entry:
            entry["createdAt"] = datetime.now(timezone.utc).isoformat()
        _feedback_entries.append(entry)
        _save_feedback()
    return entry


def get_latest_by_phone(phone: str) -> Optional[dict]:
    """Get the most recent feedback entry for a phone number."""
    with _lock:
        matches = [e for e in _feedback_entries if e.get("phone", "").lower() == phone.lower()]
        if not matches:
            return None
        return max(matches, key=lambda e: e.get("createdAt", ""))


def get_recent(count: int = 50) -> list[dict]:
    """Get recent feedback entries, newest first."""
    with _lock:
        sorted_entries = sorted(_feedback_entries, key=lambda e: e.get("createdAt", ""), reverse=True)
        return sorted_entries[:count]


def update_feedback_comment(phone: str, comment: Optional[str], category: Optional[str]) -> Optional[dict]:
    """Update the comment on the latest feedback for a phone. Returns the updated entry."""
    with _lock:
        entry = get_latest_by_phone(phone)
        if entry:
            entry["comment"] = comment
            entry["commentCategory"] = category
            entry["updatedAt"] = datetime.now(timezone.utc).isoformat()
            _save_feedback()
        return entry


def reset_user(phone: str):
    """Remove all feedback and state for a phone (testing)."""
    with _lock:
        _feedback_entries[:] = [e for e in _feedback_entries if e.get("phone", "").lower() != phone.lower()]
        _user_states.pop(phone.lower(), None)
        _save_feedback()
        _save_states()


# ── User state operations ──────────────────────────────────────────────

def get_state(phone: str) -> Optional[dict]:
    """Get the current state for a phone number."""
    with _lock:
        return _user_states.get(phone.lower())


def set_state(phone: str, state: str, meal_name: str = ""):
    """Set the state for a phone number."""
    with _lock:
        _user_states[phone.lower()] = {
            "phone": phone,
            "state": state,
            "mealName": meal_name,
        }
        _save_states()


def remove_state(phone: str):
    """Remove the state for a phone number."""
    with _lock:
        _user_states.pop(phone.lower(), None)
        _save_states()
