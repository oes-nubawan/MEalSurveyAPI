"""
JSON file-based storage for feedback entries, user states, and surveys.

Thread-safe with a REENTRANT lock (RLock). Data persists to data/ directory.
Uses atomic writes to prevent corruption on worker crashes.

Duplicate prevention is date-based: one response per phone per surveyDate.
A new survey for a different date allows a new response.

Key data:
- Surveys: batch of surveys sent to multiple phones for a meal+date
- Feedback: individual user responses linked to surveyDate
- States: per-phone conversation state machine (SURVEY_SENT, WAITING_COMMENT, etc.)
"""

import json
import os
import threading
import tempfile
from datetime import datetime, timezone
from typing import Optional


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback-data.json")
STATES_FILE = os.path.join(DATA_DIR, "user-states.json")
SURVEYS_FILE = os.path.join(DATA_DIR, "surveys.json")

# Use RLock (reentrant) so nested calls within the same thread don't deadlock
_lock = threading.RLock()

# In-memory caches (loaded from disk on startup)
_feedback_entries: list[dict] = []
_user_states: dict[str, dict] = {}  # phone (lowercase) -> state dict
_surveys: list[dict] = []  # list of survey batches


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"[store] Error loading {path}: {e}")
    return default


def _atomic_save_json(path: str, data):
    """Write JSON atomically using temp file + rename to prevent corruption."""
    try:
        _ensure_data_dir()
        dir_name = os.path.dirname(path)
        # Write to temp file first
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            # Atomic rename
            os.replace(tmp_path, path)
        except Exception:
            # Clean up temp file on error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[store] Error saving {path}: {e}")


def init():
    """Initialize storage — load data from disk."""
    global _feedback_entries, _user_states, _surveys
    _ensure_data_dir()
    _feedback_entries = _load_json(FEEDBACK_FILE, [])
    _surveys = _load_json(SURVEYS_FILE, [])
    state_list = _load_json(STATES_FILE, [])
    _user_states = {s["phone"].lower(): s for s in state_list}
    print(f"[store] Loaded {len(_feedback_entries)} feedback, {len(_user_states)} states, {len(_surveys)} surveys")


def reload_if_empty():
    """Reload data from disk if in-memory caches are empty (worker restart recovery)."""
    if not _feedback_entries and not _user_states:
        print("[store] In-memory caches empty, reloading from disk...")
        init()


# ── Survey batch operations ────────────────────────────────────────────

def add_survey(survey: dict) -> dict:
    """Add a survey batch (sent to multiple phones). Auto-increments ID."""
    with _lock:
        next_id = max((s.get("id", 0) for s in _surveys), default=0) + 1
        survey["id"] = next_id
        if "createdAt" not in survey:
            survey["createdAt"] = datetime.now(timezone.utc).isoformat()
        _surveys.append(survey)
        _atomic_save_json(SURVEYS_FILE, _surveys)
    return survey


def get_surveys(count: int = 20) -> list[dict]:
    """Get recent surveys, newest first."""
    with _lock:
        return sorted(_surveys, key=lambda s: s.get("createdAt", ""), reverse=True)[:count]


# ── Feedback operations ────────────────────────────────────────────────

def add_feedback(entry: dict) -> dict:
    """Add a feedback entry. Auto-increments ID. Returns the entry with ID."""
    with _lock:
        next_id = max((e.get("id", 0) for e in _feedback_entries), default=0) + 1
        entry["id"] = next_id
        if "createdAt" not in entry:
            entry["createdAt"] = datetime.now(timezone.utc).isoformat()
        _feedback_entries.append(entry)
        _atomic_save_json(FEEDBACK_FILE, _feedback_entries)
    return entry


def get_latest_by_phone(phone: str) -> Optional[dict]:
    """Get the most recent feedback entry for a phone number (any date)."""
    # No lock needed — reads are safe with RLock held by caller, or standalone
    matches = [e for e in _feedback_entries if e.get("phone", "").lower() == phone.lower()]
    if not matches:
        return None
    return max(matches, key=lambda e: e.get("createdAt", ""))


def get_latest_by_phone_and_date(phone: str, survey_date: str) -> Optional[dict]:
    """Get the most recent feedback entry for a phone number on a specific survey date."""
    matches = [
        e for e in _feedback_entries
        if e.get("phone", "").lower() == phone.lower()
        and e.get("surveyDate") == survey_date
    ]
    if not matches:
        return None
    return max(matches, key=lambda e: e.get("createdAt", ""))


def get_recent(count: int = 50) -> list[dict]:
    """Get recent feedback entries, newest first."""
    with _lock:
        return sorted(_feedback_entries, key=lambda e: e.get("createdAt", ""), reverse=True)[:count]


def update_feedback_comment(phone: str, comment: Optional[str], category: Optional[str]) -> Optional[dict]:
    """Update the comment on the latest feedback for a phone. Returns the updated entry."""
    with _lock:
        # Use internal read functions directly (already under _lock via RLock)
        matches = [e for e in _feedback_entries if e.get("phone", "").lower() == phone.lower()]
        if not matches:
            print(f"[store] update_feedback_comment: No feedback found for {phone}")
            return None
        entry = max(matches, key=lambda e: e.get("createdAt", ""))
        entry["comment"] = comment
        entry["commentCategory"] = category
        entry["updatedAt"] = datetime.now(timezone.utc).isoformat()
        _atomic_save_json(FEEDBACK_FILE, _feedback_entries)
    return entry


def has_incomplete_not_ok(phone: str, survey_date: str) -> Optional[dict]:
    """Check if user has a partial not_ok entry (no comment) for a date. Returns the entry or None."""
    with _lock:
        entry = get_latest_by_phone_and_date(phone, survey_date)
        if entry and entry.get("rating") == "not_ok" and entry.get("comment") is None:
            return entry
        return None


def has_any_incomplete_not_ok(phone: str) -> Optional[dict]:
    """Check if user has any partial not_ok entry (no comment) regardless of date.
    Used for recovery when state was lost and we don't know the survey date."""
    with _lock:
        matches = [
            e for e in _feedback_entries
            if e.get("phone", "").lower() == phone.lower()
            and e.get("rating") == "not_ok"
            and e.get("comment") is None
        ]
        if not matches:
            return None
        return max(matches, key=lambda e: e.get("createdAt", ""))


def reset_user(phone: str):
    """Remove all feedback and state for a phone (testing)."""
    with _lock:
        _feedback_entries[:] = [e for e in _feedback_entries if e.get("phone", "").lower() != phone.lower()]
        _user_states.pop(phone.lower(), None)
        _atomic_save_json(FEEDBACK_FILE, _feedback_entries)
        _atomic_save_json(STATES_FILE, list(_user_states.values()))


# ── User state operations ──────────────────────────────────────────────

def get_state(phone: str) -> Optional[dict]:
    """Get the current state for a phone number."""
    with _lock:
        result = _user_states.get(phone.lower())
        print(f"[store] get_state('{phone}') -> key='{phone.lower()}' result={result}")
        return result


def set_state(phone: str, state: str, meal_name: str = "", survey_date: str = ""):
    """Set the state for a phone number, including survey date."""
    with _lock:
        state_dict = {
            "phone": phone,
            "state": state,
            "mealName": meal_name,
            "surveyDate": survey_date,
        }
        _user_states[phone.lower()] = state_dict
        _atomic_save_json(STATES_FILE, list(_user_states.values()))
        print(f"[store] set_state('{phone}', '{state}', meal='{meal_name}', date='{survey_date}') saved")


def remove_state(phone: str):
    """Remove the state for a phone number."""
    with _lock:
        _user_states.pop(phone.lower(), None)
        _atomic_save_json(STATES_FILE, list(_user_states.values()))
        print(f"[store] remove_state('{phone}')")


def get_all_state_keys() -> list[str]:
    """Get all phone keys in the state dict (for debugging)."""
    with _lock:
        return list(_user_states.keys())
