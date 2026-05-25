from pathlib import Path

from voice.agents.registry import FEMALE_VOICES

GREETING_PATH_UR = Path("media/restaurant_greeting_ur.wav")
GREETING_PATH_EN = Path("media/restaurant_greeting_en.wav")
GREETING_PATH = GREETING_PATH_UR

GREETING_PROMPT = (
    "## GREETING ALREADY DONE\n"
    "A pre-recorded welcome greeting has ALREADY been played. You have ALREADY introduced yourself.\n"
    "Wait in silence for the customer's request. When the customer mentions an item:\n"
    "1. Speak a filler line in Roman Urdu: 'Ek minute, main menu check kar rahi hoon.'\n"
    "2. Call the menu tool immediately.\n"
    "YOU MUST RESPOND UNMISTAKABLY IN ROMAN URDU."
)

GREETING_PROMPT_EN = (
    "## GREETING ALREADY DONE\n"
    "A pre-recorded welcome greeting has ALREADY been played. You have ALREADY introduced yourself.\n"
    "Wait in silence for the customer's request. When the customer mentions an item:\n"
    "1. Speak a filler line: 'One moment, let me check the menu.'\n"
    "2. Call the menu tool immediately.\n"
    "RESPOND UNMISTAKABLY IN ENGLISH."
)


def get_greeting_path(language: str = "ur-PK", voice: str = "Puck") -> Path:
    lang_tag = "en" if language == "en-US" else "ur"
    return Path(f"media/restaurant_greeting_{lang_tag}_{voice}.wav")


def get_greeting_prompt(language: str = "ur-PK") -> str:
    if language == "en-US":
        return GREETING_PROMPT_EN
    return GREETING_PROMPT


def get_generate_greeting_prompt(language: str = "ur-PK", voice: str = "Puck") -> str:
    is_female = voice in FEMALE_VOICES
    if language == "en-US":
        name = "Zara" if is_female else "Ali"
        return (
            "This is the very start of the conversation. No greeting has been played yet. "
            f"You are {name}. "
            "You MUST speak a warm greeting to the customer RIGHT NOW before doing ANYTHING else. "
            "Do NOT call any tools yet. Do NOT say any filler lines. "
            "Just greet the customer warmly, for example: "
            "'Hello! Welcome to our restaurant! What would you like to order today?' "
            "Keep the greeting short and warm. Then wait for the customer to speak."
        )
    if is_female:
        name = "Zara"
        hoon_suffix = "wali hoon"
        sakti = "sakti hoon"
    else:
        name = "Ali"
        hoon_suffix = "wala hoon"
        sakti = "sakta hoon"
    return (
        "This is the very start of the conversation. No greeting has been played yet. "
        "You MUST speak a warm greeting in Roman Urdu (Urdu written in English script) RIGHT NOW. "
        "Introduce yourself as the BlenSpark Cafe ordering assistant. "
        f"Example: 'Assalam-o-alaikum! BlenSpark Cafe mein khush-amdeed! "
        f"Main {name} hoon, aap ka order lene {hoon_suffix}. Aap ki kaise madad kar {sakti}?' "
        "Keep the greeting short and professional. Then wait for the customer to speak."
    )
