"""
whatsapp_calls.py — WhatsApp Business Calling API WebRTC Bridge
Connects incoming WhatsApp calls to existing Sara/Zara Gemini Live agents.

Flow:
  WhatsApp caller → SDP offer via webhook
  → aiortc creates WebRTC connection
  → Audio piped to Django Channels consumer (ws://localhost/ws/voice/sara/)
  → Agent audio piped back to caller
"""

import asyncio
import json
import logging
import os
from fractions import Fraction
from typing import Optional

import aiohttp
import numpy as np
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    MediaStreamTrack,
    RTCIceCandidate,
    RTCConfiguration,
    RTCIceServer,
)
from av import AudioFrame

logger = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────

META_GRAPH_URL = "https://graph.facebook.com/v21.0"
ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "")

logger.info(f"📞 WhatsApp Calls initialized — token_set={bool(ACCESS_TOKEN)}")

# Django Channels agent WebSocket URLs — must match voice/routing.py paths
# Valid agent IDs: 'restaurant', 'healthcare' (see voice/agents/registry.py)
# Sara  → restaurant agent  |  Zara → healthcare agent
# ✅ Port 8000 — Django dev server; override via env var in production
AGENT_SARA_WS_URL = os.environ.get("AGENT_SARA_WS_URL", "ws://localhost:8000/ws/voice/restaurant/")
AGENT_ZARA_WS_URL = os.environ.get("AGENT_ZARA_WS_URL", "ws://localhost:8000/ws/voice/healthcare/")

# Active calls: { call_id: WhatsAppCallSession }
active_calls: dict[str, "WhatsAppCallSession"] = {}


# ─── AUDIO TRACK: WHATSAPP → AGENT ─────────────────────────────────────────────

class WhatsAppAudioReceiver(MediaStreamTrack):
    """Receives PCM audio from WhatsApp caller, forwards to agent WebSocket."""

    kind = "audio"

    def __init__(self, track: MediaStreamTrack, session: "WhatsAppCallSession"):
        super().__init__()
        self._track = track
        self._session = session
        self._running = True
        
        from av.audio.resampler import AudioResampler
        # WhatsApp WebRTC (aiortc) decodes Opus to 48kHz
        # We need 16kHz, 16-bit PCM mono for Gemini
        self.resampler = AudioResampler(format="s16", layout="mono", rate=16000)

    async def recv(self):
        frame: AudioFrame = await self._track.recv()
        bridge = self._session.ws_bridge

        if bridge and self._running:
            try:
                # Resample and format convert using PyAV's built-in resampler
                resampled_frames = self.resampler.resample(frame)
                for out_frame in resampled_frames:
                    pcm_bytes = out_frame.to_ndarray().tobytes()
                    await bridge.send_audio(pcm_bytes)
            except Exception as e:
                logger.error(f"Audio forward error: {e}", exc_info=True)

        return frame

    def stop(self):
        self._running = False
        super().stop()


# ─── AUDIO TRACK: AGENT → WHATSAPP ─────────────────────────────────────────────

class AgentAudioSender(MediaStreamTrack):
    """Receives PCM audio from agent, sends to WhatsApp caller via WebRTC."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._in_rate = 24000
        self._out_rate = 16000
        self._in_samples = 480    # 20 ms at 24 kHz
        self._out_samples = 320   # 20 ms at 16 kHz
        self._frame_bytes = self._in_samples * 2
        self._pcm_buffer = bytearray()
        self._clock_start: float | None = None
        self._clock_ts: int = 0

    async def push_audio(self, pcm_bytes: bytes):
        """Append 24 kHz int16 PCM from agent to ring buffer."""
        self._pcm_buffer.extend(pcm_bytes)

    async def recv(self) -> AudioFrame:
        """
        Return next 20 ms frame downsampled 24 kHz → 16 kHz.
        Timing mirrors aiortc's AudioStreamTrack — wall-clock based,
        no dependency on next_timestamp() which varies across aiortc versions.
        """
        import time as _time
        if self._clock_start is None:
            self._clock_start = _time.time()
            self._clock_ts = 0
        else:
            self._clock_ts += self._out_samples
            target = self._clock_start + self._clock_ts / self._out_rate
            wait = target - _time.time()
            if wait > 0:
                await asyncio.sleep(wait)

        if len(self._pcm_buffer) >= self._frame_bytes:
            raw = bytes(self._pcm_buffer[: self._frame_bytes])
            del self._pcm_buffer[: self._frame_bytes]
        else:
            raw = bytes(self._pcm_buffer) + bytes(self._frame_bytes - len(self._pcm_buffer))
            self._pcm_buffer.clear()

        # Downsample 24 kHz → 16 kHz (3:2 ratio, linear interpolation)
        samples_in = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        x_in  = np.arange(self._in_samples, dtype=np.float32)
        x_out = np.linspace(0.0, self._in_samples - 1, self._out_samples)
        samples_out = np.interp(x_out, x_in, samples_in).astype(np.int16)

        frame = AudioFrame.from_ndarray(samples_out.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self._out_rate
        frame.pts = self._clock_ts
        frame.time_base = Fraction(1, self._out_rate)
        return frame


# ─── WEBSOCKET BRIDGE ───────────────────────────────────────────────────────────

class WebSocketBridge:
    """Manages WebSocket connection to Django Channels agent consumer."""

    def __init__(self, call_id: str, caller_number: str, agent_sender: AgentAudioSender):
        self.call_id = call_id
        self.caller_number = caller_number
        self.agent_sender = agent_sender
        self.ws = None
        self._running = True
        self._session_ready = False  # set True when BrowserVoiceConsumer signals ready
        self._on_call_end = None  # callback when agent signals task complete

    def set_on_call_end(self, callback):
        """Set callback to invoke when agent signals task complete."""
        self._on_call_end = callback

    async def connect(self, agent_ws_url: str):
        """Connect to Django Channels consumer."""
        try:
            import websockets
            self.ws = await websockets.connect(agent_ws_url)
            logger.info(f"[{self.call_id}] Connected to agent: {agent_ws_url}")
            asyncio.ensure_future(self._listen_agent())
        except Exception as e:
            logger.error(f"[{self.call_id}] WebSocket connect failed: {e}")
            raise

    async def send_audio(self, pcm_bytes: bytes):
        """Send caller audio to agent — only after session_ready signal."""
        if self.ws and self._running and self._session_ready:
            try:
                await self.ws.send(pcm_bytes)
            except Exception as e:
                logger.error(f"[{self.call_id}] Send audio failed: {e}")

    async def _listen_agent(self):
        """
        Listen for agent output.
        BrowserVoiceConsumer sends JSON control frames first, then binary PCM.
        We must wait for 'session_ready' before forwarding caller audio,
        otherwise BrowserVoiceConsumer silently drops all received bytes.

        Control messages:
        - session_ready / audio_config: agent is ready for audio
        - clear: agent barge-in detected
        - call_end: task completed, end call gracefully
        """
        if not self.ws:
            return

        try:
            async for message in self.ws:
                if isinstance(message, str):
                    # JSON control message from BrowserVoiceConsumer
                    try:
                        import json as _json
                        data = _json.loads(message)
                        event = data.get("event", "")

                        if event in ("session_ready", "audio_config"):
                            self._session_ready = True
                            logger.info(f"[{self.call_id}] ✅ Agent session ready (event='{event}') — forwarding caller audio")
                        elif event == "clear":
                            logger.info(f"[{self.call_id}] Agent barge-in — clearing audio queue")
                        elif event == "call_end":
                            reason = data.get("reason", "task_complete")
                            logger.info(f"[{self.call_id}] 👋 Agent signaled call end: {reason}")
                            if self._on_call_end:
                                await self._on_call_end(reason)
                            return
                        # ignore ping and other control frames
                    except Exception:
                        pass
                elif isinstance(message, bytes) and self._running:
                    # Binary PCM16 from Gemini → push to AgentAudioSender for WebRTC
                    await self.agent_sender.push_audio(message)
        except Exception as e:
            if self._running:
                logger.error(f"[{self.call_id}] Listen agent failed: {e}")

    async def close(self):
        self._running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass


# ─── SDP DIAGNOSTICS ────────────────────────────────────────────────────────────

def _log_sdp_diagnostics(call_id: str, sdp: str):
    """
    Log the full SDP answer at INFO level and report key attributes.
    This is essential for diagnosing Meta SDP validation errors.
    """
    lines = sdp.splitlines()
    candidates = [l for l in lines if l.startswith("a=candidate")]
    fingerprint = [l for l in lines if l.startswith("a=fingerprint")]
    setup       = [l for l in lines if l.startswith("a=setup")]
    rtpmap      = [l for l in lines if l.startswith("a=rtpmap")]
    ice_ufrag   = [l for l in lines if l.startswith("a=ice-ufrag")]
    ice_pwd     = [l for l in lines if l.startswith("a=ice-pwd")]
    bundle      = [l for l in lines if "BUNDLE" in l]
    mid         = [l for l in lines if l.startswith("a=mid")]

    logger.info(
        f"[{call_id}] 🔍 SDP DIAGNOSTICS:\n"
        f"  Total lines  : {len(lines)}\n"
        f"  BUNDLE       : {bundle}\n"
        f"  a=mid        : {mid}\n"
        f"  Candidates   : {len(candidates)} found → {candidates}\n"
        f"  Fingerprint  : {fingerprint}\n"
        f"  Setup role   : {setup}\n"
        f"  ICE ufrag    : {ice_ufrag}\n"
        f"  ICE pwd      : {ice_pwd}\n"
        f"  Codecs       : {rtpmap}"
    )
    # Print the full SDP so we can paste it into an SDP analyser if needed
    logger.info(f"[{call_id}] 🔍 FULL SDP ANSWER:\n{sdp}")


def _clean_sdp_for_meta(sdp: str) -> str:
    """
    Post-process aiortc's SDP answer to satisfy Meta's strict SDP validator.

    Root cause of error 138008:
      aiortc emits THREE a=fingerprint lines (sha-256, sha-384, sha-512).
      Meta's validator rejects any SDP with more than one fingerprint attribute.
      We keep only the sha-256 line, which is all Meta needs.

    Also normalises line-endings to CRLF (\\r\\n) as RFC 4566 requires.
    """
    raw_lines = sdp.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    cleaned: list[str] = []
    fingerprint_kept = False

    for line in raw_lines:
        if line.startswith("a=fingerprint:"):
            if not fingerprint_kept:
                cleaned.append(line)   # keep sha-256 (first one aiortc emits)
                fingerprint_kept = True
            # discard the extra sha-384 and sha-512 lines
        else:
            cleaned.append(line)

    result = "\r\n".join(cleaned) + "\r\n"
    logger.debug(f"_clean_sdp_for_meta: removed {len(raw_lines) - len(cleaned)} extra fingerprint line(s)")
    return result


class WhatsAppCallSession:
    """Manages one WhatsApp call end-to-end: WebRTC ↔ Django agent."""

    def __init__(self, call_id: str, caller_number: str, agent: str = "sara"):
        self.call_id = call_id
        self.caller_number = caller_number
        self.agent_name = agent
        self.pc: Optional[RTCPeerConnection] = None
        self.ws_bridge: Optional[WebSocketBridge] = None
        self.audio_sender = AgentAudioSender()
        self._running = True

    async def handle_offer(self, sdp_offer: str) -> str:
        """Process SDP offer from WhatsApp, return SDP answer with ICE candidates."""

        # ✅ FIX 1: Provide STUN so aiortc discovers the server's public IP.
        #    Replace with a TURN server if the host is behind a symmetric NAT.
        config = RTCConfiguration(iceServers=[
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
        ])
        self.pc = RTCPeerConnection(configuration=config)

        # Add outgoing audio track (agent → caller)
        self.pc.addTrack(self.audio_sender)

        # ✅ FIX 2: Event to know when ICE gathering is done
        ice_gathered = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        def _on_ice_gathering():
            state = self.pc.iceGatheringState
            logger.info(f"[{self.call_id}] ICE gathering state → {state}")
            if state == "complete":
                ice_gathered.set()

        @self.pc.on("icecandidate")
        def _on_candidate(candidate):
            if candidate:
                logger.info(f"[{self.call_id}] 🧊 ICE candidate: {candidate.candidate}")
            else:
                logger.info(f"[{self.call_id}] 🧊 ICE gathering end-of-candidates signal")

        @self.pc.on("track")
        async def on_track(track):
            """Handle incoming audio from caller."""
            if track.kind == "audio":
                logger.info(f"[{self.call_id}] 🎙️  Audio track received from WhatsApp")

                # Start relay immediately — don't block on agent connection.
                # AgentAudioSender sends silence until agent sends real audio.
                receiver = WhatsAppAudioReceiver(track, self)  # ✅ pass session, not ws_bridge
                self._relay_task = asyncio.create_task(self._relay_incoming(receiver))

                # Connect to agent in background so on_track never raises
                self._connect_task = asyncio.create_task(self._connect_agent_background())

        @self.pc.on("connectionstatechange")
        async def on_state():
            state = self.pc.connectionState
            logger.info(f"[{self.call_id}] WebRTC state: {state}")
            if state in ("failed", "closed", "disconnected"):
                await self.cleanup()

        # Set remote description (SDP offer from WhatsApp)
        logger.info(f"[{self.call_id}] Setting remote SDP offer")
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp_offer, type="offer")
        )

        # Create answer and set local description — this triggers ICE gathering
        logger.info(f"[{self.call_id}] Creating SDP answer...")
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        # ✅ FIX 3: Wait for ICE gathering to complete (up to 5 s) so the
        #    answer actually contains reachable candidates before sending to Meta.
        try:
            await asyncio.wait_for(ice_gathered.wait(), timeout=5.0)
            logger.info(f"[{self.call_id}] ✅ ICE gathering complete")
        except asyncio.TimeoutError:
            logger.warning(f"[{self.call_id}] ⚠️  ICE gathering timed out — sending with available candidates")

        sdp_answer = self.pc.localDescription.sdp
        logger.info(f"[{self.call_id}] ✅ SDP answer ready (length={len(sdp_answer)})")

        # ── DIAGNOSTIC: log full SDP answer at INFO so we can see it in logs ──
        _log_sdp_diagnostics(self.call_id, sdp_answer)

        # ── CLEAN: strip extra fingerprints that Meta's validator rejects ──
        sdp_answer = _clean_sdp_for_meta(sdp_answer)
        logger.info(f"[{self.call_id}] ✅ Cleaned SDP (length={len(sdp_answer)}) — sending to Meta")
        logger.info(f"[{self.call_id}] 🔍 CLEANED SDP:\n{sdp_answer}")

        return sdp_answer

    async def _connect_agent(self):
        """Connect to Django Channels consumer (Sara or Zara)."""
        agent_ws_url = (
            AGENT_SARA_WS_URL
            if self.agent_name == "sara"
            else AGENT_ZARA_WS_URL
        )

        try:
            self.ws_bridge = WebSocketBridge(self.call_id, self.caller_number, self.audio_sender)

            # Set callback: when agent signals call_end, terminate gracefully
            async def on_agent_call_end(reason: str):
                logger.info(f"[{self.call_id}] Agent signaled end: {reason} — closing call gracefully")
                await end_call_api(self.call_id)
                await self.cleanup()

            self.ws_bridge.set_on_call_end(on_agent_call_end)
            await self.ws_bridge.connect(agent_ws_url)
        except Exception as e:
            logger.error(f"[{self.call_id}] Agent connect failed: {e}")
            raise

    async def _connect_agent_background(self):
        """Connect to agent WebSocket in background — never raises, so RTP silence keeps flowing."""
        try:
            await self._connect_agent()
            logger.info(f"[{self.call_id}] ✅ Agent connected — real audio will now replace silence")
        except Exception as e:
            logger.error(
                f"[{self.call_id}] ❌ Agent connection failed: {e}\n"
                f"  Check AGENT_SARA_WS_URL env var (current default: ws://localhost:8000/ws/voice/sara/)\n"
                f"  Silence will continue to be sent to the caller."
            )

    async def _relay_incoming(self, receiver: WhatsAppAudioReceiver):
        """Forward WhatsApp audio frames to agent."""
        while self._running:
            try:
                await receiver.recv()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.error(f"[{self.call_id}] Incoming relay error: {e}", exc_info=True)
                break

    async def cleanup(self):
        self._running = False
        
        # Cancel any pending background tasks
        if hasattr(self, '_relay_task') and self._relay_task:
            self._relay_task.cancel()
        if hasattr(self, '_connect_task') and self._connect_task:
            self._connect_task.cancel()

        if self.ws_bridge:
            await self.ws_bridge.close()

        if self.pc and self.pc.connectionState != "closed":
            await self.pc.close()

        active_calls.pop(self.call_id, None)
        logger.info(f"[{self.call_id}] Call session cleaned up")


# ─── META GRAPH API HELPERS ────────────────────────────────────────────────────

async def end_call_api(call_id: str) -> bool:
    """Terminate call via Meta API."""
    url = f"{META_GRAPH_URL}/{_phone_number_id()}/calls"
    payload = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "terminate",
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=15) as resp:
                result = await resp.json()
                if result.get("success"):
                    logger.info(f"[{call_id}] ✅ Call terminated via API")
                    return True
                else:
                    logger.error(f"[{call_id}] Terminate failed: {result}")
                    return False
    except Exception as e:
        logger.error(f"[{call_id}] end_call_api error: {e}")
        return False


# ─── MAIN EVENT HANDLER ─────────────────────────────────────────────────────────

async def pre_accept_call(call_id: str, sdp_answer: str) -> bool:
    """Pre-accept call with SDP answer (recommended by Meta)."""
    url = f"{META_GRAPH_URL}/{_phone_number_id()}/calls"
    payload = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "pre_accept",
        "session": {"sdp_type": "answer", "sdp": sdp_answer},
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    logger.info(f"[{call_id}] Sending pre_accept — SDP length={len(sdp_answer)}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=15) as resp:
                result = await resp.json()
                if result.get("success"):
                    logger.info(f"[{call_id}] ✅ Pre-accepted call")
                    return True
                else:
                    logger.error(f"[{call_id}] Pre-accept failed: {result}")
                    # Log the full SDP that was rejected so we can diagnose
                    logger.error(f"[{call_id}] Rejected SDP was:\n{sdp_answer}")
                    return False
    except Exception as e:
        logger.error(f"[{call_id}] pre_accept_call error: {e}")
        return False


async def accept_call(call_id: str, sdp_answer: str) -> bool:
    """Accept call after WebRTC connection established."""
    url = f"{META_GRAPH_URL}/{_phone_number_id()}/calls"
    payload = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "accept",
        "session": {"sdp_type": "answer", "sdp": sdp_answer},
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=15) as resp:
                result = await resp.json()
                if result.get("success"):
                    logger.info(f"[{call_id}] ✅ Accepted call — media flowing")
                    return True
                else:
                    logger.error(f"[{call_id}] Accept failed: {result}")
                    return False
    except Exception as e:
        logger.error(f"[{call_id}] accept_call error: {e}")
        return False


async def handle_call_event(event: dict):
    """
    Process a call webhook event from Meta.

    Event types:
    - connect: incoming call with SDP offer
    - terminate: call ended
    """
    calls_list = event.get("calls", [])
    if not calls_list:
        logger.warning("⚠️  No calls in webhook event")
        return

    for call_info in calls_list:
        call_id = call_info.get("id", "")
        event_type = call_info.get("event", "")
        caller_number = call_info.get("from", "")
        session_data = call_info.get("session", {})
        sdp_offer = session_data.get("sdp")

        logger.info(f"📞 Call event | ID: {call_id} | Event: {event_type} | From: {caller_number}")

        if event_type == "connect" and sdp_offer:
            # New incoming call — create session and prepare answer
            session = WhatsAppCallSession(call_id=call_id, caller_number=caller_number, agent="sara")
            active_calls[call_id] = session

            try:
                # Create SDP answer via aiortc/WebRTC (now waits for ICE gathering)
                sdp_answer = await session.handle_offer(sdp_offer)

                # ✅ Step 1: pre_accept with SDP answer (required by Meta)
                pre_accepted = await pre_accept_call(call_id, sdp_answer)
                if not pre_accepted:
                    logger.error(f"[{call_id}] pre_accept failed — terminating")
                    await end_call_api(call_id)
                    await session.cleanup()
                    return

                # ✅ Step 2: Wait briefly for WebRTC to connect, then accept
                await asyncio.sleep(1.5)
                accepted = await accept_call(call_id, sdp_answer)
                if not accepted:
                    logger.error(f"[{call_id}] accept failed — terminating")
                    await end_call_api(call_id)
                    await session.cleanup()
                    return

                logger.info(f"[{call_id}] ✅ Call accepted — waiting for agent connection")

            except Exception as e:
                logger.error(f"[{call_id}] Failed to handle connect: {e}", exc_info=True)
                await end_call_api(call_id)

        elif event_type == "terminate":
            # Call ended
            session = active_calls.get(call_id)
            if session:
                await session.cleanup()
            logger.info(f"[{call_id}] Call terminated")


def _phone_number_id() -> str:
    """Get business phone number ID from environment."""
    return os.environ.get("META_PHONE_NUMBER_ID", "")
