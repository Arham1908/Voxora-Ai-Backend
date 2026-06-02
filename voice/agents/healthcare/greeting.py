from pathlib import Path

from voice.agents.registry import FEMALE_VOICES

GREETING_PATH = Path("media/healthcare_greeting_ur.wav")

GREETING_PROMPT = (
    "The system has already played a welcome greeting to the user. "
    "Wait in silence for the user to speak their request. "
    "When they say what they need, say your filler line 'ایک لمحہ، میں چیک کر رہا ہوں۔' and then call the appropriate tool. "
    "Do NOT speak anything until the user has spoken first."
)

GREETING_PROMPT_EN = (
    "The system has already played a welcome greeting to the user. "
    "Wait in silence for the user to speak their request. "
    "When they say what they need, say your filler line 'One moment, let me check.' and then call the appropriate tool. "
    "Do NOT speak anything until the user has spoken first."
)


def get_greeting_path(language: str = "ur-PK", voice: str = "Puck") -> Path:
    lang_tag = "en" if language == "en-US" else "ur"
    return Path(f"media/healthcare_greeting_{lang_tag}_{voice}.wav")


def get_greeting_prompt(language: str = "ur-PK") -> str:
    if language == "en-US":
        return GREETING_PROMPT_EN
    return GREETING_PROMPT


def get_generate_greeting_prompt(language: str = "ur-PK", voice: str = "Puck") -> str:
    is_female = voice in FEMALE_VOICES
    if language == "en-US":
        name = "Sara" if is_female else "Alex"
        return (
            "This is the very start of the conversation. No greeting has been played yet. "
            f"You are {name}. "
            "You MUST speak exactly this greeting RIGHT NOW before doing ANYTHING else: "
            f"'Hello! Thank you for calling BlenSpark Clinic. My name is {name}, your virtual health assistant. I'm here to help you book an appointment today. May I please have your full name to get started?' "
            "You MUST speak strictly in English. DO NOT speak in Urdu. "
            "Do NOT call any tools yet. Do NOT say any filler lines. "
            "Just speak this greeting exactly as written and wait for the user to respond."
        )
    if is_female:
        name = "Sara"
        madad_kaise = "kaise madad kar sakti hoon"
    else:
        name = "Ali"
        madad_kaise = "kaise madad kar sakta hoon"
    return (
        "This is the very start of the conversation. No greeting has been played yet. "
        "You MUST speak a warm greeting in Roman Urdu (Urdu written in English characters) RIGHT NOW. "
        "Mixing some English words like 'appointment' or 'assistant' is encouraged for better pronunciation. "
        f"Greeting: 'Assalam-o-alaikum! Mera naam {name} hai. Bataiye, main aap ki appointment booking mein {madad_kaise}?' "
        "Do NOT call any tools yet. Just speak this greeting and wait for the patient to respond."
    )
