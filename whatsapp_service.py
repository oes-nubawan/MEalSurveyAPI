"""
WhatsApp Business API service.
Sends interactive list messages, button messages, templates, and plain text.
"""

import os
import requests
import json


class WhatsAppService:
    def __init__(self):
        self.token = os.environ.get("WHATSAPP_TOKEN", "")
        self.phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.api_version = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
        self.template_name = os.environ.get("WHATSAPP_TEMPLATE_NAME", "meal_survey")
        self.template_language = os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "en_US")

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: dict):
        """Send a POST to WhatsApp API."""
        print(f"[wa] POST {self.base_url}")
        print(f"[wa] Payload: {json.dumps(payload, indent=2)}")
        try:
            resp = requests.post(self.base_url, headers=self.headers, json=payload, timeout=10)
            print(f"[wa] Response: {resp.status_code} {resp.text[:500]}")
            if resp.status_code >= 400:
                print(f"[wa] ERROR: {resp.status_code} {resp.text}")
                raise Exception(f"WhatsApp API error {resp.status_code}: {resp.text[:200]}")
            return resp
        except Exception as e:
            print(f"[wa] Request failed: {e}")
            raise

    # ── Plain text message ─────────────────────────────────────────────

    def send_text(self, to: str, message: str):
        """Send a plain text WhatsApp message."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message},
        }
        return self._post(payload)

    # ── Survey: Interactive List (4 options) ────────────────────────────
    # This is the PRIMARY survey type — supports all 4 ratings.

    def send_survey_list(self, to: str, meal_name: str):
        """Send an interactive list message with 4 rating options."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": f"How was today's meal?\n{meal_name}"},
                "action": {
                    "button": "Rate Meal",
                    "sections": [
                        {
                            "title": "Rate the meal",
                            "rows": [
                                {"id": "very_good", "title": "Very Good"},
                                {"id": "good", "title": "Good"},
                                {"id": "satisfactory", "title": "Satisfactory"},
                                {"id": "not_ok", "title": "Not Acceptable"},
                            ],
                        }
                    ],
                },
            },
        }
        return self._post(payload)

    # ── Survey: Interactive Buttons (max 3) ─────────────────────────────

    def send_survey_buttons(self, to: str, meal_name: str):
        """Send button message with 3 options (WhatsApp limit). Good is excluded."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": f"How was today's meal?\n{meal_name}"},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "very_good", "title": "Very Good"}},
                        {"type": "reply", "reply": {"id": "satisfactory", "title": "Satisfactory"}},
                        {"type": "reply", "reply": {"id": "not_ok", "title": "Not Acceptable"}},
                    ]
                },
            },
        }
        return self._post(payload)

    # ── Survey: Two-Step (rate then optional comment) ───────────────────

    def send_survey_two_step(self, to: str, meal_name: str):
        """Send a two-step list survey."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": f"Rate today's meal: {meal_name}\nStep 1 of 2 - Choose a rating"},
                "action": {
                    "button": "Rate Meal",
                    "sections": [
                        {
                            "title": "Rate the meal",
                            "rows": [
                                {"id": "ts_very_good", "title": "Very Good"},
                                {"id": "ts_good", "title": "Good"},
                                {"id": "ts_satisfactory", "title": "Satisfactory"},
                                {"id": "ts_not_ok", "title": "Not Acceptable"},
                            ],
                        }
                    ],
                },
            },
        }
        return self._post(payload)

    # ── Survey: Utility Template with quick-reply buttons ───────────────

    def send_survey_template(self, to: str, meal_name: str):
        """Send a utility template message with quick-reply buttons."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.template_language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {
                                "type": "text",
                                "parameter_name": "meal_name",
                                "text": meal_name,
                            }
                        ],
                    }
                ],
            },
        }
        return self._post(payload)

    # ── Default survey (list) ──────────────────────────────────────────

    def send_survey(self, to: str, meal_name: str, survey_type: str = "list"):
        """Send a survey by type."""
        if survey_type == "button":
            return self.send_survey_buttons(to, meal_name)
        elif survey_type == "twostep":
            return self.send_survey_two_step(to, meal_name)
        elif survey_type == "template":
            return self.send_survey_template(to, meal_name)
        else:
            return self.send_survey_list(to, meal_name)
