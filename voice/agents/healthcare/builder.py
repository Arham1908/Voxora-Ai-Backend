from datetime import datetime
from zoneinfo import ZoneInfo

from voice.agents.registry import FEMALE_VOICES
from .config import GENDER_TOKENS, DAY_NAMES
from .templates import URDU_TEMPLATE, ENGLISH_TEMPLATE


def _time_to_spoken(time_str: str) -> str:
    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        period = "AM" if hour < 12 else "PM"
        display_hour = hour % 12 or 12
        if minute == 0:
            return f"{display_hour} {period}"
        return f"{display_hour}:{minute:02d} {period}"
    except (ValueError, IndexError):
        return time_str


def _format_schedule_block(schedule_data: list) -> str:
    if not schedule_data:
        return ""
    lines = ["# Pre-loaded Weekly Schedule (from database)"]
    lines.append("You already have the schedule below. Use it directly — no need to call get_schedule unless you want to refresh.")
    lines.append("IMPORTANT: When speaking times to the user, say them naturally (e.g. '9 AM', '5 PM', '9:30 AM').")
    lines.append("NEVER read out leading zeros — say 'nine AM' not 'zero nine AM'.")
    lines.append("")
    for entry in schedule_data:
        day_num = entry.get("day_of_week", -1)
        day_name = DAY_NAMES.get(day_num, f"Day {day_num}")
        active = entry.get("is_active", False)
        start_raw = entry.get("start_time", "?")
        end_raw = entry.get("end_time", "?")
        start = _time_to_spoken(start_raw)
        end = _time_to_spoken(end_raw)
        duration = entry.get("slot_duration", 30)
        status_str = "OPEN" if active else "CLOSED"
        if active:
            lines.append(f"- {day_name} ({day_num}): {status_str} | {start} – {end} | slot = {duration} mins")
        else:
            lines.append(f"- {day_name} ({day_num}): {status_str}")
    lines.append("")
    return "\n".join(lines)


def build_system_prompt(language: str = "ur-PK", voice: str = "Puck", has_cached_greeting: bool = False, schedule_data: list = None) -> str:
    now = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%A, %B %d, %Y %I:%M %p")
    is_female = voice in FEMALE_VOICES
    gender = "female" if is_female else "male"

    if language == "en-US":
        return _build_english_prompt(now, gender, has_cached_greeting, schedule_data)
    return _build_urdu_prompt(now, gender, has_cached_greeting, schedule_data)


def _build_urdu_prompt(now: str, gender: str, has_cached_greeting: bool, schedule_data: list = None) -> str:
    tokens = dict(GENDER_TOKENS[("ur-PK", gender)])
    tokens["now"] = now
    tokens["schedule_block"] = _format_schedule_block(schedule_data)

    if has_cached_greeting:
        tokens["greeting_context"] = (
            "## GREETING ALREADY DONE\n"
            "A pre-recorded welcome greeting has ALREADY been played. You have ALREADY introduced yourself.\n"
            "NEVER say Assalam-o-alaikum again. NEVER re-introduce yourself. NEVER repeat what the greeting said.\n"
            "IMPORTANT RULES FOR YOUR FIRST RESPONSE:\n"
            "- YOU MUST RESPOND UNMISTAKABLY IN ROMAN URDU.\n"
            f"- If the user ONLY replies with a greeting (like 'wa salam', 'theek hoon'), respond briefly: 'Shukriya! Bataein, main aapki appointment ke liye kaise madad kar {tokens['sakti_hoon']}?'\n"
            "- If the user mentions 'appointment' or a scheduling request (even alongside a greeting reply), "
            "SKIP the help-offer. Say the filler line first. Then call get_schedule immediately.\n"
            f"- NEVER say 'kya madad kar {tokens['sakti_hoon']}' if the user already told you what they want.\n"
            "- Keep your first response to ONE short sentence max."
        )
    else:
        tokens["greeting_context"] = ""

    return URDU_TEMPLATE.format(**tokens)


def _build_english_prompt(now: str, gender: str, has_cached_greeting: bool, schedule_data: list = None) -> str:
    tokens = dict(GENDER_TOKENS[("en-US", gender)])
    tokens["now"] = now
    tokens["schedule_block"] = _format_schedule_block(schedule_data)
    tokens["gender_desc_lower"] = tokens["gender_desc"].lower()

    if has_cached_greeting:
        tokens["greeting_context"] = (
            "## GREETING ALREADY DONE — DO NOT GREET AGAIN\n"
            "A pre-recorded welcome greeting has ALREADY been played. You have ALREADY introduced yourself.\n"
            "NEVER say Hello, Welcome, Hi, or any greeting.\n"
            "NEVER introduce yourself again — the user already knows who you are.\n"
            "Wait in COMPLETE SILENCE for the user to speak first.\n"
            "Your first words must ONLY be a direct response to what the user says.\n"
            "RESPOND UNMISTAKABLY IN ENGLISH."
        )
    else:
        tokens["greeting_context"] = ""

    return ENGLISH_TEMPLATE.format(**tokens)
