"""
Supabase-backed storage for meal survey application.
Uses PostgREST API directly (no extra SDK needed).

Tables:
- tbl_meal: meals served (meal_date, meal_name)
- tbl_users: registered users (name, phoneno, availstatus)
- tbl_usersresponse: user responses — ONE row per user per date
  response_status flow: pending → rated → completed

The database IS the state machine. No in-memory state needed.
"""

import os
import requests
from datetime import datetime, timezone
from typing import Optional


class SupabaseStore:
    def __init__(self):
        self.url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        self.key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        self._headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _get(self, table: str, params=None) -> list[dict]:
        url = f"{self.url}/rest/v1/{table}"
        try:
            resp = requests.get(url, headers=self._headers, params=params, timeout=10)
            if resp.status_code >= 400:
                print(f"[store] GET {table} error {resp.status_code}: {resp.text[:200]}")
                return []
            return resp.json() if resp.text else []
        except Exception as e:
            print(f"[store] GET {table} exception: {e}")
            return []

    def _post(self, table: str, data: dict) -> list[dict]:
        url = f"{self.url}/rest/v1/{table}"
        try:
            resp = requests.post(url, headers=self._headers, json=data, timeout=10)
            if resp.status_code >= 400:
                print(f"[store] POST {table} error {resp.status_code}: {resp.text[:200]}")
                raise Exception(f"Supabase insert error: {resp.text[:200]}")
            return resp.json() if resp.text else []
        except Exception as e:
            if "Supabase" in str(e):
                raise
            print(f"[store] POST {table} exception: {e}")
            raise

    def _patch(self, table: str, data: dict, params=None) -> list[dict]:
        url = f"{self.url}/rest/v1/{table}"
        try:
            resp = requests.patch(url, headers=self._headers, json=data, params=params, timeout=10)
            if resp.status_code >= 400:
                print(f"[store] PATCH {table} error {resp.status_code}: {resp.text[:200]}")
                raise Exception(f"Supabase update error: {resp.text[:200]}")
            return resp.json() if resp.text else []
        except Exception as e:
            if "Supabase" in str(e):
                raise
            print(f"[store] PATCH {table} exception: {e}")
            raise

    def _delete(self, table: str, params=None) -> list[dict]:
        url = f"{self.url}/rest/v1/{table}"
        try:
            resp = requests.delete(url, headers=self._headers, params=params, timeout=10)
            if resp.status_code >= 400:
                print(f"[store] DELETE {table} error {resp.status_code}: {resp.text[:200]}")
                raise Exception(f"Supabase delete error: {resp.text[:200]}")
            return resp.json() if resp.text else []
        except Exception as e:
            if "Supabase" in str(e):
                raise
            print(f"[store] DELETE {table} exception: {e}")
            raise

    # ── Init ────────────────────────────────────────────────────────

    def init(self):
        """Verify Supabase connection."""
        if not self.url or not self.key:
            raise Exception(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables are required. "
                "Set them in Render dashboard."
            )
        try:
            self._get("tbl_users", {"select": "id", "limit": "1"})
            print(f"[store] Supabase connected: {self.url}")
        except Exception as e:
            print(f"[store] Supabase connection FAILED: {e}")

    def reload_if_empty(self):
        """No-op — Supabase data is always in the database."""
        pass

    # ── Users ────────────────────────────────────────────────────────

    def get_user_by_phone(self, phone: str) -> Optional[dict]:
        """Find a user by phone number."""
        clean = phone.strip().replace("+", "").replace(" ", "")
        results = self._get("tbl_users", {"phoneno": f"eq.{clean}", "select": "*"})
        return results[0] if results else None

    def get_available_users(self) -> list[dict]:
        """Get all users where availstatus = true."""
        return self._get("tbl_users", {"availstatus": "eq.true", "select": "*", "order": "name.asc"})

    def get_all_users(self) -> list[dict]:
        """Get all users."""
        return self._get("tbl_users", {"select": "*", "order": "name.asc"})

    def add_user(self, name: str, phoneno: str, availstatus: bool = True) -> dict:
        """Add a new user."""
        clean = phoneno.strip().replace("+", "").replace(" ", "")
        result = self._post("tbl_users", {
            "name": name,
            "phoneno": clean,
            "availstatus": availstatus,
        })
        return result[0] if result else {}

    def update_user(self, user_id: str, **kwargs) -> dict:
        """Update a user (name, phoneno, availstatus)."""
        result = self._patch("tbl_users", kwargs, {"id": f"eq.{user_id}"})
        return result[0] if result else {}

    def delete_user(self, user_id: str):
        """Delete a user (cascades to responses)."""
        self._delete("tbl_users", {"id": f"eq.{user_id}"})

    # ── Meals ────────────────────────────────────────────────────────

    def add_meal(self, meal_date: str, meal_name: str) -> dict:
        """Add a meal. Returns existing if duplicate."""
        # Check if already exists
        existing = self._get("tbl_meal", {
            "meal_date": f"eq.{meal_date}",
            "meal_name": f"eq.{meal_name}",
        })
        if existing:
            return existing[0]
        result = self._post("tbl_meal", {
            "meal_date": meal_date,
            "meal_name": meal_name,
        })
        return result[0] if result else {}

    def get_meals(self, count: int = 20) -> list[dict]:
        """Get recent meals."""
        return self._get("tbl_meal", {"select": "*", "order": "meal_date.desc", "limit": str(count)})

    def get_meal_by_date(self, meal_date: str) -> Optional[dict]:
        """Get the meal for a specific date. Returns the most recent meal entry for that date."""
        results = self._get("tbl_meal", {
            "meal_date": f"eq.{meal_date}",
            "order": "created_at.desc",
            "limit": "1",
        })
        return results[0] if results else None

    # ── Meal Availability (per-user per-date) ──────────────────────

    def get_available_users_for_date(self, meal_date: str) -> list[dict]:
        """Get users who availed the meal on a specific date (availstatus=true in tbl_mealavail).
        Also creates rows for any users who don't have an entry yet (defaults to false).
        Returns user records enriched with their mealavail id and status."""
        # Get all users
        all_users = self._get("tbl_users", {"select": "*", "order": "name.asc"})
        if not all_users:
            return []

        user_ids = [u["id"] for u in all_users]

        # Get existing mealavail entries for this date
        existing = self._get("tbl_mealavail", {
            "meal_date": f"eq.{meal_date}",
            "user_id": f"in.({','.join(user_ids)})",
            "select": "*",
        })
        existing_map = {e["user_id"]: e for e in existing}

        # Create rows for users who don't have one yet (default availstatus=false)
        for user in all_users:
            if user["id"] not in existing_map:
                try:
                    result = self._post("tbl_mealavail", {
                        "meal_date": meal_date,
                        "user_id": user["id"],
                        "availstatus": False,
                    })
                    if result:
                        existing_map[user["id"]] = result[0]
                except Exception as e:
                    print(f"[store] Error creating mealavail for user {user['id']}: {e}")

        # Return only users with availstatus=true
        available = []
        for user in all_users:
            avail_entry = existing_map.get(user["id"])
            if avail_entry and avail_entry.get("availstatus"):
                user["mealavail_id"] = avail_entry["id"]
                user["mealavail_status"] = True
                available.append(user)

        return available

    def get_mealavail_for_date(self, meal_date: str) -> list[dict]:
        """Get all mealavail entries for a date, enriched with user name and phone."""
        entries = self._get("tbl_mealavail", {
            "meal_date": f"eq.{meal_date}",
            "select": "*",
            "order": "created_at.asc",
        })
        if not entries:
            return []

        # Enrich with user data
        user_ids = list(set(e["user_id"] for e in entries))
        users = self._get("tbl_users", {
            "select": "id,name,phoneno",
            "id": f"in.({','.join(user_ids)})",
        })
        user_map = {u["id"]: u for u in users}

        for e in entries:
            user = user_map.get(e["user_id"], {})
            e["name"] = user.get("name", "")
            e["phoneno"] = user.get("phoneno", "")

        return entries

    def set_mealavail(self, meal_date: str, user_id: str, availstatus: bool) -> Optional[dict]:
        """Set a user's meal availability for a specific date. Creates row if not exists."""
        # Try to update existing
        existing = self._get("tbl_mealavail", {
            "meal_date": f"eq.{meal_date}",
            "user_id": f"eq.{user_id}",
        })
        if existing:
            result = self._patch("tbl_mealavail", {"availstatus": availstatus}, {
                "meal_date": f"eq.{meal_date}",
                "user_id": f"eq.{user_id}",
            })
            return result[0] if result else None
        else:
            # Create new entry
            result = self._post("tbl_mealavail", {
                "meal_date": meal_date,
                "user_id": user_id,
                "availstatus": availstatus,
            })
            return result[0] if result else None

    def set_mealavail_bulk(self, meal_date: str, user_ids: list[str], availstatus: bool) -> int:
        """Set mealavail for multiple users on a date. Returns count of updated/created."""
        count = 0
        for uid in user_ids:
            try:
                self.set_mealavail(meal_date, uid, availstatus)
                count += 1
            except Exception as e:
                print(f"[store] Error setting mealavail for user {uid}: {e}")
        return count

    # ── Responses ────────────────────────────────────────────────────

    def create_response(self, user_id: str, meal_date: str, meal_name: str) -> Optional[dict]:
        """Create a pending response. Returns None if already exists (duplicate)."""
        # Check existing first to avoid UNIQUE constraint violation
        existing = self._get("tbl_usersresponse", {
            "user_id": f"eq.{user_id}",
            "meal_date": f"eq.{meal_date}",
        })
        if existing:
            return None  # Already has a response for this date
        try:
            result = self._post("tbl_usersresponse", {
                "user_id": user_id,
                "meal_date": meal_date,
                "meal_name": meal_name,
                "response_status": "pending",
            })
            return result[0] if result else None
        except Exception as e:
            if "duplicate" in str(e).lower() or "unique" in str(e).lower() or "409" in str(e):
                return None
            raise

    def has_response(self, user_id: str, meal_date: str) -> bool:
        """Check if a response already exists for this user+date. No creation."""
        existing = self._get("tbl_usersresponse", {
            "user_id": f"eq.{user_id}",
            "meal_date": f"eq.{meal_date}",
            "select": "id",
        })
        return len(existing) > 0

    def delete_response(self, user_id: str, meal_date: str) -> bool:
        """Delete a response row — used to clean up after a failed WhatsApp send."""
        try:
            self._delete("tbl_usersresponse", {
                "user_id": f"eq.{user_id}",
                "meal_date": f"eq.{meal_date}",
            })
            return True
        except Exception as e:
            print(f"[store] Error deleting response for user {user_id}: {e}")
            return False

    def get_active_response(self, user_id: str) -> Optional[dict]:
        """Get the active (pending or rated) response for a user. Most recent first."""
        results = self._get("tbl_usersresponse", {
            "user_id": f"eq.{user_id}",
            "response_status": "in.(pending,rated)",
            "order": "meal_date.desc",
            "limit": "1",
        })
        return results[0] if results else None

    def get_response_by_user_and_date(self, user_id: str, meal_date: str) -> Optional[dict]:
        """Get a specific response."""
        results = self._get("tbl_usersresponse", {
            "user_id": f"eq.{user_id}",
            "meal_date": f"eq.{meal_date}",
        })
        return results[0] if results else None

    def get_latest_response_by_phone(self, phone: str) -> Optional[dict]:
        """Get the latest response for a phone number (any status)."""
        user = self.get_user_by_phone(phone)
        if not user:
            return None
        results = self._get("tbl_usersresponse", {
            "user_id": f"eq.{user['id']}",
            "order": "meal_date.desc",
            "limit": "1",
        })
        return results[0] if results else None

    def update_response(self, user_id: str, meal_date: str, **kwargs) -> Optional[dict]:
        """Update a response by user_id and meal_date."""
        result = self._patch("tbl_usersresponse", kwargs, {
            "user_id": f"eq.{user_id}",
            "meal_date": f"eq.{meal_date}",
        })
        return result[0] if result else None

    def get_recent_responses(self, count: int = 50) -> list[dict]:
        """Get recent responses enriched with user name and phone."""
        responses = self._get("tbl_usersresponse", {
            "select": "*",
            "order": "created_at.desc",
            "limit": str(count),
        })
        if not responses:
            return []
        # Enrich with user data
        user_ids = list(set(r["user_id"] for r in responses))
        users = self._get("tbl_users", {
            "select": "id,name,phoneno",
            "id": f"in.({','.join(user_ids)})",
        })
        user_map = {u["id"]: u for u in users}
        for r in responses:
            user = user_map.get(r["user_id"], {})
            r["phone"] = user.get("phoneno", "")
            r["userName"] = user.get("name", "")
        return responses

    def get_surveys(self, count: int = 20) -> list[dict]:
        """Get recent meals (survey batches)."""
        return self._get("tbl_meal", {
            "select": "*",
            "order": "created_at.desc",
            "limit": str(count),
        })

    def reset_user_responses(self, phone: str):
        """Delete all responses for a user by phone (testing)."""
        user = self.get_user_by_phone(phone)
        if not user:
            return
        self._delete("tbl_usersresponse", {"user_id": f"eq.{user['id']}"})

    def has_incomplete_not_ok(self, phone: str) -> Optional[dict]:
        """Check if user has a not_ok response without remarks."""
        user = self.get_user_by_phone(phone)
        if not user:
            return None
        results = self._get("tbl_usersresponse", {
            "user_id": f"eq.{user['id']}",
            "user_response": "eq.not_ok",
            "remarks": "is.null",
            "order": "meal_date.desc",
            "limit": "1",
        })
        return results[0] if results else None

    def get_all_state_keys(self) -> list[str]:
        """Get phones with active responses (debug helper)."""
        results = self._get("tbl_usersresponse", {
            "response_status": "in.(pending,rated)",
            "select": "user_id",
        })
        if not results:
            return []
        user_ids = list(set(r["user_id"] for r in results))
        users = self._get("tbl_users", {
            "select": "phoneno",
            "id": f"in.({','.join(user_ids)})",
        })
        return [u["phoneno"] for u in users]

    # ── Legacy compatibility (used by conversation.py) ──────────────

    def add_feedback(self, entry: dict) -> dict:
        """Compatibility: insert or update a response from conversation handler."""
        user = self.get_user_by_phone(entry.get("phone", ""))
        if not user:
            print(f"[store] add_feedback: User not found for phone {entry.get('phone')}")
            return entry
        user_id = user["id"]
        meal_date = entry.get("surveyDate", "")
        meal_name = entry.get("mealName", "")
        rating = entry.get("rating", "")
        comment = entry.get("comment")

        # Check if response exists
        existing = self.get_response_by_user_and_date(user_id, meal_date)
        if existing:
            # Update existing
            now = datetime.now(timezone.utc).isoformat()
            update_data = {
                "user_response": rating,
                "response_status": "completed" if (rating != "not_ok" or comment is not None) else "rated",
                "response_datetime": now,
            }
            if comment is not None:
                update_data["remarks"] = comment
            self.update_response(user_id, meal_date, **update_data)
            return entry
        else:
            # Create new
            now = datetime.now(timezone.utc).isoformat()
            self.create_response(user_id, meal_date, meal_name)
            update_data = {
                "user_response": rating,
                "response_status": "completed" if (rating != "not_ok" or comment is not None) else "rated",
                "response_datetime": now,
            }
            if comment is not None:
                update_data["remarks"] = comment
            self.update_response(user_id, meal_date, **update_data)
            return entry

    def update_feedback_comment(self, phone: str, comment: Optional[str], category: Optional[str]) -> Optional[dict]:
        """Compatibility: update remarks on the latest response for a phone."""
        user = self.get_user_by_phone(phone)
        if not user:
            print(f"[store] update_feedback_comment: User not found for phone {phone}")
            return None
        # Find latest not_ok response without remarks
        results = self._get("tbl_usersresponse", {
            "user_id": f"eq.{user['id']}",
            "user_response": "eq.not_ok",
            "order": "meal_date.desc",
            "limit": "1",
        })
        if not results:
            return None
        entry = results[0]
        now = datetime.now(timezone.utc).isoformat()
        update_data = {
            "remarks": comment,
            "response_status": "completed",
            "response_datetime": now,
        }
        result = self.update_response(user["id"], entry["meal_date"], **update_data)
        return result

    def get_latest_by_phone(self, phone: str) -> Optional[dict]:
        """Compatibility: get latest response for a phone."""
        return self.get_latest_response_by_phone(phone)

    def get_latest_by_phone_and_date(self, phone: str, survey_date: str) -> Optional[dict]:
        """Compatibility: get latest response for a phone and date."""
        user = self.get_user_by_phone(phone)
        if not user:
            return None
        return self.get_response_by_user_and_date(user["id"], survey_date)

    def add_survey(self, survey: dict) -> dict:
        """Compatibility: add a meal entry."""
        meal = self.add_meal(survey.get("surveyDate", ""), survey.get("mealName", ""))
        return meal

    def get_recent(self, count: int = 50) -> list[dict]:
        """Compatibility: get recent responses."""
        responses = self.get_recent_responses(count)
        # Map to old format for UI
        result = []
        for i, r in enumerate(responses):
            result.append({
                "id": r.get("id", "")[:8],
                "phone": r.get("phone", ""),
                "userName": r.get("userName", ""),
                "mealName": r.get("meal_name", ""),
                "surveyDate": r.get("meal_date", ""),
                "rating": r.get("user_response", ""),
                "comment": r.get("remarks"),
                "responseStatus": r.get("response_status", ""),
                "createdAt": r.get("response_datetime") or r.get("created_at", ""),
            })
        return result

    def get_recent_by_date(self, meal_date: str, count: int = 50) -> list[dict]:
        """Get recent responses filtered by date — server-side filter saves bandwidth."""
        # Get responses for this date
        responses = self._get("tbl_usersresponse", {
            "meal_date": f"eq.{meal_date}",
            "select": "*",
            "order": "created_at.desc",
            "limit": str(count),
        })
        if not responses:
            return []
        # Enrich with user data
        user_ids = list(set(r["user_id"] for r in responses))
        users = self._get("tbl_users", {
            "select": "id,name,phoneno",
            "id": f"in.({','.join(user_ids)})",
        })
        user_map = {u["id"]: u for u in users}
        result = []
        for r in responses:
            user = user_map.get(r["user_id"], {})
            result.append({
                "id": r.get("id", "")[:8],
                "phone": user.get("phoneno", ""),
                "userName": user.get("name", ""),
                "mealName": r.get("meal_name", ""),
                "surveyDate": r.get("meal_date", ""),
                "rating": r.get("user_response", ""),
                "comment": r.get("remarks"),
                "responseStatus": r.get("response_status", ""),
                "createdAt": r.get("response_datetime") or r.get("created_at", ""),
            })
        return result

    def reset_user(self, phone: str):
        """Compatibility: reset all responses for a user."""
        self.reset_user_responses(phone)

    def cleanup_orphaned_pending(self) -> int:
        """Delete pending responses older than 2 hours — they were likely never delivered.
        
        A pending response that's been sitting for 2+ hours without any user
        interaction means the WhatsApp message was probably never sent (old bug)
        or the user ignored it. Deleting these allows re-sending.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        # Find old pending responses
        old_pending = self._get("tbl_usersresponse", {
            "response_status": "eq.pending",
            "created_at": f"lt.{cutoff}",
            "select": "id,user_id,meal_date",
        })
        if not old_pending:
            return 0
        count = 0
        for entry in old_pending:
            try:
                self._delete("tbl_usersresponse", {"id": f"eq.{entry['id']}"})
                count += 1
                print(f"[store] Cleaned orphaned pending: user={entry['user_id']}, date={entry['meal_date']}")
            except Exception as e:
                print(f"[store] Error cleaning orphan: {e}")
        return count

    # No-op methods (state is in DB now)
    def get_state(self, phone: str) -> Optional[dict]:
        """No-op — state is in the database."""
        return None

    def set_state(self, phone: str, state: str, meal_name: str = "", survey_date: str = ""):
        """No-op — state is in the database."""
        pass

    def remove_state(self, phone: str):
        """No-op — state is in the database."""
        pass


# Module-level singleton
store = SupabaseStore()
