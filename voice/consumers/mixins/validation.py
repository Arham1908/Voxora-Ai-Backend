import re

from ...audio.utils import clean_transcript_text, looks_like_transcript_noise


class ValidationMixin:
    _call_history: list
    _last_user_transcript: str

    def _user_transcript_text(self) -> str:
        return " ".join(
            clean_transcript_text(item.get("text", ""))
            for item in self._call_history
            if item.get("role") == "user"
        ).casefold()

    @staticmethod
    def _digits_only(value) -> str:
        return re.sub(r"\D", "", str(value or ""))

    @staticmethod
    def _email_match_text(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").casefold())

    def _validate_terminal_tool_args(self, tool_name: str, tool_args: dict) -> dict | None:
        if tool_name != "book_appointment":
            return None
        user_text = self._user_transcript_text()
        user_text_email = self._email_match_text(user_text)
        user_digits = self._digits_only(user_text)
        missing = []
        name = str(tool_args.get("name") or "").strip()
        name_tokens = re.findall(r"[a-z0-9]{2,}", name.casefold())
        if not name or (name_tokens and not all(t in user_text for t in name_tokens)):
            missing.append("name")
        phone_digits = self._digits_only(tool_args.get("phone"))
        if len(phone_digits) < 10 or phone_digits not in user_digits:
            missing.append("phone")
        email = str(tool_args.get("email") or "").strip().casefold()
        has_at = "@" in email
        domain = email.split("@")[1] if has_at else ""
        if not has_at or not (email in user_text_email or (domain and domain in user_text_email)):
            missing.append("email")
        for field in ("date", "start_time", "end_time"):
            if not str(tool_args.get(field) or "").strip():
                missing.append(field)
        if not missing:
            return None
        return {
            "error": True, "code": "unsafe_hallucinated_booking_details",
            "missing_fields": list(dict.fromkeys(missing)),
            "message": "Booking was not saved because required details were not clearly provided.",
            "retry_instruction": "Ask the patient for missing details, one at a time.",
        }

    def _record_user_transcript(self, text: str) -> bool:
        cleaned = clean_transcript_text(text)
        if looks_like_transcript_noise(cleaned):
            return False
        last = self._last_user_transcript
        if last:
            if cleaned == last or cleaned in last:
                return False
            if last in cleaned and self._call_history and self._call_history[-1].get("role") == "user":
                self._call_history[-1]["text"] = cleaned
                self._last_user_transcript = cleaned
                return True
        self._call_history.append({"role": "user", "text": cleaned})
        self._last_user_transcript = cleaned
        return True
