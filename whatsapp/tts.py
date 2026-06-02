import os
import re
import uuid
import logging
import requests

log = logging.getLogger(__name__)

def strip_markdown_and_emojis(text: str) -> str:
    """Removes standard markdown asterisks, underscores, and emojis for better TTS readout."""
    # Remove basic markdown
    text = re.sub(r'[*_~`]', '', text)
    # Remove emojis (a basic regex matching typical emoji unicode blocks)
    text = re.sub(r'[^\w\s,.\-!?"\']+', ' ', text)
    return text.strip()

def generate_tts_audio(text: str) -> str:
    """
    Synthesize text using ElevenLabs API and return the local path of the downloaded .ogg file.
    Returns None if an error occurs.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "kD4dEWy2fbcyXlge6iHh")  # Default to a generic voice if not found
    
    if not api_key:
        log.warning("No ELEVENLABS_API_KEY found in .env, skipping TTS generation.")
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": api_key
    }

    # Clean the text perfectly
    clean_text = strip_markdown_and_emojis(text)
    if not clean_text:
        return None

    # We want output_format=mp3_44100_128 or ogg_opus. GreenAPI supports mp3/ogg.
    # We will use ogg just to be safe as WhatsApp heavily standardizes on OGG for OPUS voice notes.
    payload = {
        "text": clean_text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }
    
    # Optional: we can ask ElevenLabs for ogg output via query parameter 
    # ?output_format=ogg_22050_242
    req_url = f"{url}?output_format=mp3_44100_128"

    try:
        response = requests.post(req_url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        
        # Save audio chunk temporarily
        filename = f"/tmp/whatsapp_voice_{uuid.uuid4().hex}.mp3"
        
        # Ensure tmp exists
        os.makedirs("/tmp", exist_ok=True)
        
        with open(filename, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
                    
        return filename
    except Exception as e:
        log.error("TTS synthesis failed: %s", e)
        return None
