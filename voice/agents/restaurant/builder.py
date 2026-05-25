from datetime import datetime
from zoneinfo import ZoneInfo

from voice.agents.registry import FEMALE_VOICES
from .config import GENDER_TOKENS
from .templates import URDU_TEMPLATE, ENGLISH_TEMPLATE


def build_system_prompt(language: str = "ur-PK", voice: str = "Puck", has_cached_greeting: bool = False, **kwargs) -> str:
    now = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%A, %B %d, %Y %I:%M %p")
    is_female = voice in FEMALE_VOICES
    gender = "female" if is_female else "male"

    if language == "en-US":
        return _build_english_prompt(now, gender, has_cached_greeting)
    return _build_urdu_prompt(now, gender, has_cached_greeting)


def _build_urdu_prompt(now: str, gender: str, has_cached_greeting: bool) -> str:
    tokens = dict(GENDER_TOKENS[("ur-PK", gender)])
    tokens["now"] = now
    tokens["gender_desc_lower"] = tokens["gender_desc"].lower()

    if has_cached_greeting:
        tokens["greeting_context"] = (
            "## GREETING ALREADY DONE\n"
            "A pre-recorded welcome greeting has ALREADY been played. You have ALREADY introduced yourself.\n"
            "NEVER say Assalam-o-alaikum again. NEVER re-introduce yourself. NEVER repeat what the greeting said.\n"
            "IMPORTANT RULES FOR YOUR FIRST RESPONSE:\n"
            "- If the customer ONLY replies with a greeting (like 'wa salam', 'theek hoon'), respond briefly: 'Shukriya! Bataein, kya order karna chahain gay?'\n"
            "- If the customer mentions any food item or says 'I want to order' (even alongside a greeting), "
            "SKIP the help-offer. Say the filler line first. Then call the menu tool immediately.\n"
            "- NEVER say 'kya madad kar sakta/sakti hoon' if the customer already told you what they want.\n"
            "- Keep your first response to ONE short sentence max."
        )
    else:
        tokens["greeting_context"] = ""

    return URDU_TEMPLATE.format(**tokens)


def _build_english_prompt(now: str, gender: str, has_cached_greeting: bool) -> str:
    tokens = dict(GENDER_TOKENS[("en-US", gender)])
    tokens["now"] = now
    tokens["gender_desc_lower"] = tokens["gender_desc"].lower()

    if has_cached_greeting:
        tokens["greeting_context"] = (
            "## GREETING ALREADY DONE\n"
            "A pre-recorded welcome greeting has ALREADY been played. You have ALREADY introduced yourself.\n"
            "NEVER say Hello, Welcome, Hi, or any greeting again. NEVER re-introduce yourself.\n"
            "IMPORTANT RULES FOR YOUR FIRST RESPONSE:\n"
            "- If the customer ONLY replies with a greeting, respond briefly: 'Thank you! What would you like to order?'\n"
            "- If the customer mentions any food item or says 'I want to order' (even alongside a greeting), "
            "SKIP the help-offer and go straight to fetching the menu.\n"
            "- NEVER ask 'how can I help' if the customer already told you what they want.\n"
            "- Keep your first response to ONE short sentence max."
        )
    else:
        tokens["greeting_context"] = ""

    return ENGLISH_TEMPLATE.format(**tokens)
