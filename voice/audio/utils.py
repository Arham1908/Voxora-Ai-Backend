import audioop
import base64
import logging
import re
import wave
from pathlib import Path

SIP_RATE = 8000
MIC_RATE = 16000
OUT_RATE = 24000

logger = logging.getLogger(__name__)

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_WHITESPACE_RE = re.compile(r"\s+")
_SHORT_DEVANAGARI_KEEP = {"हेलो", "हैलो", "नमस्ते", "सलाम", "अस्सलाम"}


def clean_transcript_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def looks_like_transcript_noise(text: str) -> bool:
    cleaned = clean_transcript_text(text)
    if not cleaned:
        return True
    tokens = cleaned.casefold().split()
    unique_tokens = set(tokens)
    normalized = cleaned.casefold().strip("।.!? ")
    if normalized in _SHORT_DEVANAGARI_KEEP:
        return False
    if len(tokens) >= 4 and len(unique_tokens) <= 2:
        return True
    if len(tokens) <= 2 and _DEVANAGARI_RE.search(cleaned):
        ascii_or_urdu = re.search(r"[A-Za-z\u0600-\u06FF]", cleaned)
        if not ascii_or_urdu:
            return True
    return False


def pcm_to_mulaw(pcm_24k: bytes) -> bytes:
    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, OUT_RATE, SIP_RATE, None)
    return audioop.lin2ulaw(pcm_8k, 2)


def load_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        return wf.readframes(wf.getnframes())


def save_wav(pcm_data: bytes, path: Path, sample_rate: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    logger.info(f"Saved WAV: {path} ({sample_rate}Hz, {len(pcm_data)} bytes)")


def twilio_payload_to_pcm16k(payload_b64: str, ratecv_state=None):
    mulaw_8k = base64.b64decode(payload_b64)
    pcm_8k = audioop.ulaw2lin(mulaw_8k, 2)
    pcm_16k, new_state = audioop.ratecv(pcm_8k, 2, 1, SIP_RATE, MIC_RATE, ratecv_state)
    return pcm_16k, new_state


def pcm16k_to_twilio_payload(pcm_16k: bytes, ratecv_state=None):
    pcm_8k, new_state = audioop.ratecv(pcm_16k, 2, 1, MIC_RATE, SIP_RATE, ratecv_state)
    mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
    return base64.b64encode(mulaw_8k).decode("ascii"), new_state
