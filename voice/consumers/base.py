import asyncio
import base64
import json
import logging
import os
import time
import urllib.parse
import uuid

from channels.generic.websocket import AsyncWebsocketConsumer
from websockets.exceptions import ConnectionClosed
from google.oauth2 import service_account
import vertexai
import truststore
import audioop
truststore.inject_into_ssl()

from google import genai
from google.genai import types

# ── Schedule cache ────────────────────────────────────────────────────────────
_schedule_cache: dict = {"data": None, "fetched_at": 0.0}
_SCHEDULE_CACHE_TTL = 60.0

async def _cached_schedule_data() -> list:
    now = time.time()
    if _schedule_cache["data"] is not None and (now - _schedule_cache["fetched_at"]) < _SCHEDULE_CACHE_TTL:
        return _schedule_cache["data"]
    from appointment.models import Schedule
    from appointment.serializers import ScheduleSerializer
    from asgiref.sync import sync_to_async
    data = await sync_to_async(lambda: list(Schedule.objects.all()))()
    result = ScheduleSerializer(data, many=True).data
    _schedule_cache["data"] = result
    _schedule_cache["fetched_at"] = now
    return result

from ..audio.utils import (
    SIP_RATE, MIC_RATE, OUT_RATE,
    clean_transcript_text, save_wav, load_wav_pcm,
)
from .mixins import CostTrackingMixin
from .mixins import SilenceHandlerMixin
from .mixins import ValidationMixin
from ..audio.utils import twilio_payload_to_pcm16k

logger = logging.getLogger(__name__)

_raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if not _raw_json:
    raise EnvironmentError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.")

try:
    _sa_info = json.loads(_raw_json)
except json.JSONDecodeError as e:
    raise ValueError(f"Invalid JSON in GOOGLE_SERVICE_ACCOUNT_JSON: {e}") from e

_sa_info["private_key"] = _sa_info["private_key"].replace("\\n", "\n")

_credentials = service_account.Credentials.from_service_account_info(
    _sa_info, scopes=["https://www.googleapis.com/auth/cloud-platform"],
)

_PROJECT  = os.environ.get("VERTEX_PROJECT")
_LOCATION = os.environ.get("VERTEX_LOCATION")
if not _PROJECT:
    raise EnvironmentError("VERTEX_PROJECT must be set.")

vertexai.init(project=_PROJECT, location=_LOCATION, credentials=_credentials)

os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)

LIVE_MODEL        = "gemini-3.1-flash-live-preview"
VERTEX_LIVE_MODEL = "gemini-live-2.5-flash-preview-native-audio-09-2025"
VOICE_NAME        = "Aoede"


def _create_live_clients():
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        raise EnvironmentError("GEMINI_API_KEY must be set.")
    t0 = time.time()
    primary = genai.Client(api_key=gemini_api_key)
    vertex  = genai.Client(vertexai=True, project=_PROJECT, location=_LOCATION, credentials=_credentials)
    print(f"[WS Startup] Gemini clients created in {time.time()-t0:.2f}s (Direct + Vertex)", flush=True)
    return primary, vertex


_SHARED_GEMINI_CLIENT, _SHARED_VERTEX_CLIENT = _create_live_clients()





class VoiceAgentConsumer(
    CostTrackingMixin, SilenceHandlerMixin, ValidationMixin, AsyncWebsocketConsumer,
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gemini_session    = None
        self.client            = None
        self._vertex_client    = None
        self._session_ready    = asyncio.Event()
        self._disconnecting    = False
        self._tasks: list[asyncio.Task] = []
        self._save_as_greeting = False
        self._upsample_state   = None
        self._downsample_state = None
        self._session_uuid     = str(uuid.uuid4())
        self._usage_metrics    = {"prompt": 0, "response": 0, "total": 0, "input_text": 0, "input_audio": 0, "output_text": 0, "output_audio": 0}
        self._session_cost_saved = False
        self._skip_session_save_once = False
        self._start_time       = None
        self._call_history     = []
        self._last_user_transcript = ""
        self._current_agent_turn = ""
        self._should_end_call  = False
        self._auto_close_task = None
        self._auto_close_reason = ""
        self._last_session_handle = None
        self._booking_state = ""
        self._pending_tool_calls = 0
        self._transport = "browser"
        self._twilio_stream_sid = None
        self._twilio_call_sid = None
        self._twilio_ready = asyncio.Event()
        self._twilio_media_count = 0
        self._last_user_input_time = None
        self._silence_check_task = None
        self._agent_speaking = False
        self._consecutive_nudges = 0
        self._cached_menu = None
        self._audio_queue = bytearray()
        self._thinking_tokens_from_usage = 0
        self._logged_modality_sample = False

    # ── WebSocket lifecycle ───────────────────────────────────────────

    async def connect(self):
        self._start_time = time.time()
        await self.accept()
        params = dict(urllib.parse.parse_qsl(self.scope.get("query_string", b"").decode("utf-8")))
        self._transport = params.get("transport", "browser").lower()
        self.client = _SHARED_GEMINI_CLIENT
        self._vertex_client = _SHARED_VERTEX_CLIENT
        task = asyncio.create_task(self._run_gemini_session_with_fallback())
        self._tasks.append(task)

    async def receive(self, bytes_data=None, text_data=None):
        if self._disconnecting:
            return

        if text_data and self._transport != "twilio":
            try:
                probe = json.loads(text_data)
                if probe.get("event") in ("connected", "start", "media", "stop"):
                    self._transport = "twilio"
            except (json.JSONDecodeError, AttributeError):
                pass

        if self._transport == "twilio" and text_data:
            await self._handle_twilio_message(json.loads(text_data))
            return

        if not bytes_data or not self._session_ready.is_set():
            return

        session = self.gemini_session
        if session is None:
            self._clear_session_state()
            return

        pcm_8k = audioop.ulaw2lin(bytes_data, 2)
        pcm_16k, self._upsample_state = audioop.ratecv(pcm_8k, 2, 1, SIP_RATE, MIC_RATE, self._upsample_state)

        if self._pending_tool_calls > 0:
            if self._audio_queue is None:
                self._audio_queue = bytearray()
            self._audio_queue.extend(pcm_16k)
            return

        try:
            await session.send_realtime_input(audio=types.Blob(data=pcm_16k, mime_type=f"audio/pcm;rate={MIC_RATE}"))
        except Exception as exc:
            self._clear_session_state()

    async def _handle_twilio_message(self, msg: dict):
        event = msg.get("event")
        if event == "connected":
            return
        if event == "start":
            start_data = msg.get("start", {})
            self._twilio_stream_sid = start_data.get("streamSid", msg.get("streamSid"))
            self._twilio_call_sid = start_data.get("callSid", "")
            self._twilio_ready.set()
            return
        if event == "media":
            self._twilio_media_count += 1
            payload = msg.get("media", {}).get("payload", "")
            if not payload:
                return
            pcm_16k, self._upsample_state = twilio_payload_to_pcm16k(payload, self._upsample_state)
            if not self._session_ready.is_set():
                return
            session = self.gemini_session
            if session is None:
                self._clear_session_state()
                return
            if self._pending_tool_calls > 0:
                if self._audio_queue is None:
                    self._audio_queue = bytearray()
                self._audio_queue.extend(pcm_16k)
                return
            try:
                await session.send_realtime_input(audio=types.Blob(data=pcm_16k, mime_type=f"audio/pcm;rate={MIC_RATE}"))
            except Exception as exc:
                logger.error("Twilio send error: %s", exc, exc_info=True)
                self._clear_session_state()
            return
        if event == "stop":
            await self.disconnect(1000)
            return

    async def disconnect(self, close_code):
        self._disconnecting = True
        self._twilio_ready.set()
        session = self.gemini_session
        if session is not None:
            await self._close_gemini_session(session, "disconnect")
        current_task = asyncio.current_task()
        pending = [t for t in self._tasks if t is not current_task and not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        self._tasks.clear()
        self._clear_session_state()

    def _clear_session_state(self):
        self.gemini_session = None
        self._session_ready.clear()
        self._audio_queue.clear()

    async def _close_gemini_session(self, session=None, reason: str = "cleanup"):
        session = session or self.gemini_session
        if session is None:
            return
        close = getattr(session, "close", None)
        if close is None:
            return
        try:
            await asyncio.wait_for(close(), timeout=3.0)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.debug("Gemini close (%s): %s", reason, exc)

    async def _on_gemini_ready(self):
        pass

    # ── Override hooks ────────────────────────────────────────────────

    async def _fetch_schedule_data(self) -> list:
        return await _cached_schedule_data()

    def _get_system_prompt(self, has_cached_greeting: bool = False, schedule_data: list = None) -> str:
        from .agents.healthcare import build_system_prompt
        return build_system_prompt(language=self._get_language_code(), voice=self._get_voice_name(), has_cached_greeting=has_cached_greeting, schedule_data=schedule_data)

    def _get_tools(self):
        from .agents.healthcare import TOOLS
        return TOOLS

    def _get_agent_key(self) -> str:
        return "default"

    def _get_voice_name(self) -> str:
        return "Aoede"

    def _get_language_code(self) -> str:
        return "en-US"

    def _get_greeting_path(self):
        from .agents.healthcare import get_greeting_path
        return get_greeting_path(self._get_language_code(), self._get_voice_name())

    def _get_greeting_prompt(self) -> str:
        from .agents.healthcare import get_greeting_prompt
        return get_greeting_prompt(self._get_language_code())

    def _get_generate_greeting_prompt(self) -> str:
        from .agents.healthcare import get_generate_greeting_prompt
        return get_generate_greeting_prompt(self._get_language_code(), self._get_voice_name())

    async def _execute_tool(self, tool_name: str, tool_args: dict) -> dict:
        from .agents.healthcare import execute_tool
        if tool_name == "menu":
            if getattr(self, "_cached_menu", None):
                return self._cached_menu
            result = await execute_tool(tool_name, tool_args)
            if isinstance(result, dict) and result.get("success"):
                self._cached_menu = result
            return result
        return await execute_tool(tool_name, tool_args)

    # ── Gemini Live session ───────────────────────────────────────────

    @staticmethod
    def _is_503_error(exc: Exception) -> bool:
        err_str = str(exc).lower()
        err_type = type(exc).__name__.lower()
        triggers = ("503", "service unavailable", "unavailable", "model not available", "overloaded", "resource has been exhausted", "quota", "rate limit", "429", "1011", "internal error")
        return any(t in err_str for t in triggers) or "serviceunavailable" in err_type

    async def _run_gemini_session_with_fallback(self):
        try:
            await self._run_gemini_session(client=self.client, model=LIVE_MODEL)
        except Exception as primary_exc:
            if self._disconnecting:
                return
            if self._is_503_error(primary_exc):
                self._clear_session_state()
                await asyncio.sleep(0.5)
                history_snapshot = list(self._call_history)
                try:
                    await self._run_gemini_session(client=self._vertex_client, model=VERTEX_LIVE_MODEL, prior_history=history_snapshot)
                except Exception as fallback_exc:
                    self._clear_session_state()
                    await self.close()
            else:
                raise

    async def _run_gemini_session(self, client=None, model: str = None, prior_history: list = None):
        if client is None:
            client = self.client
        if model is None:
            model = LIVE_MODEL

        is_fallback = bool(prior_history)
        voice_name    = self._get_voice_name()
        language_code = self._get_language_code()
        greeting_path = self._get_greeting_path()
        has_cached_greeting = greeting_path.exists()

        schedule_data = await self._fetch_schedule_data()
        system_prompt = self._get_system_prompt(has_cached_greeting=has_cached_greeting, schedule_data=schedule_data)

        if is_fallback and prior_history:
            history_lines = []
            for entry in prior_history:
                role = entry.get("role", "unknown")
                if role == "user":
                    history_lines.append(f"Patient: {entry.get('text', '')}")
                elif role == "agent":
                    history_lines.append(f"Agent: {entry.get('text', '')}")
                elif role == "tool":
                    history_lines.append(f"[Tool '{entry.get('tool_name')}' called with {entry.get('tool_args')}, result: {entry.get('tool_result')}]")
            if history_lines:
                system_prompt += "\n\n---\n# CONVERSATION HISTORY (recorded before network failover)\n" + "\n".join(history_lines) + "\n---"

        tools = self._get_tools()
        live_config = types.LiveConnectConfig(
            system_instruction=types.Content(parts=[types.Part(text=system_prompt)]),
            response_modalities=["AUDIO"],
            temperature=0.8,
            tools=tools,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)),
                language_code=language_code,
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
            session_resumption=types.SessionResumptionConfig(handle=None if is_fallback else self._last_session_handle),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    prefix_padding_ms=100,
                    silence_duration_ms=800,
                )
            ),
        )

        # ── Try pre-warmed session ────────────────────────────────────
        from voice.consumers.prewarmer import get_prewarmer
        warm = None
        if not is_fallback:
            warm = await get_prewarmer(self._get_agent_key()).acquire()

        if warm:
            try:
                self.gemini_session = warm.session
                self._session_ready.set()
                await self._on_gemini_ready()

                await self._handle_greeting(warm.session)
                # Signal browser FE to open mic gate (mirrors line 449 cold-start path)
                if self._transport == "browser":
                    try: await self.send(text_data=json.dumps({"type": "session_ready"}))
                    except Exception: pass
                self._mark_user_input()
                self._schedule_silence_check()

                # Warm next session in background
                asyncio.create_task(
                    get_prewarmer(self._get_agent_key()).warm(client, model, live_config)
                )

                try:
                    await self._receive_loop(warm.session)
                except asyncio.CancelledError:
                    raise
                finally:
                    await warm.release()

                if not self._disconnecting:
                    await self.close()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._disconnecting:
                    return
                if not is_fallback and self._is_503_error(e):
                    self._clear_session_state()
                    self._skip_session_save_once = True
                    raise
                self._clear_session_state()
                await self.close()
            finally:
                if self._skip_session_save_once:
                    self._skip_session_save_once = False
                else:
                    await self._save_session_cost()
                self._clear_session_state()
            return

        # ── Cold start fallback ───────────────────────────────────────
        t0 = time.time()
        try:
            async with client.aio.live.connect(model=model, config=live_config) as session:
                self.gemini_session = session
                self._session_ready.set()
                await self._on_gemini_ready()

                if is_fallback:
                    await session.send_realtime_input(text="[System: You have seamlessly taken over this call. Do not re-greet. Acknowledge the brief pause and continue naturally.]")
                    if self._transport == "browser":
                        try: await self.send(text_data=json.dumps({"type": "session_ready"}))
                        except Exception: pass
                else:
                    await self._handle_greeting(session)
                    self._mark_user_input()
                    self._schedule_silence_check()

                    # Warm next session in background
                    asyncio.create_task(
                        get_prewarmer(self._get_agent_key()).warm(client, model, live_config)
                    )

                try:
                    await self._receive_loop(session)
                except asyncio.CancelledError:
                    await self._close_gemini_session(session, "receive-loop-cancel")
                    raise

                if not self._disconnecting:
                    await self.close()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._disconnecting:
                return
            if not is_fallback and self._is_503_error(e):
                self._clear_session_state()
                self._skip_session_save_once = True
                raise
            self._clear_session_state()
            await self.close()
        finally:
            if self._skip_session_save_once:
                self._skip_session_save_once = False
            else:
                await self._save_session_cost()
            self._clear_session_state()

    # ── Greeting ──────────────────────────────────────────────────────

    async def _handle_greeting(self, session):
        greeting_path   = self._get_greeting_path()
        if greeting_path.exists():
            pcm_data = load_wav_pcm(greeting_path)
            await self._stream_pcm_to_sip(pcm_data)
            try:
                await session.send_realtime_input(audio_stream_end=True)
            except Exception:
                pass
        else:
            generate_prompt = self._get_generate_greeting_prompt()
            self._save_as_greeting = True
            await session.send_realtime_input(text=generate_prompt)

    async def _stream_pcm_to_sip(self, pcm_24k: bytes):
        if self._transport == "browser":
            await self._send_audio_chunk(pcm_24k)
            return
        pcm_8k, self._downsample_state = audioop.ratecv(pcm_24k, 2, 1, OUT_RATE, SIP_RATE, self._downsample_state)
        sip_audio = audioop.lin2ulaw(pcm_8k, 2)
        await self._send_audio_chunk(sip_audio)

    async def _send_audio_chunk(self, sip_audio: bytes):
        if self._transport == "twilio":
            chunk_size = 160
            if not self._twilio_ready.is_set():
                await self._twilio_ready.wait()
            if not self._twilio_stream_sid:
                return
            for i in range(0, len(sip_audio), chunk_size):
                chunk = sip_audio[i:i + chunk_size]
                payload = base64.b64encode(chunk).decode("ascii")
                try:
                    await self.send(text_data=json.dumps({"event": "media", "streamSid": self._twilio_stream_sid, "media": {"payload": payload}}))
                except Exception:
                    break
                await asyncio.sleep(0.01)
            return
        # browser transport: use 80ms chunks (~3840 bytes at 24kHz PCM16)
        # with 5ms spacing to stream ~7× faster than real-time
        browser_chunk_ms = 80
        browser_chunk_size = OUT_RATE * 2 * browser_chunk_ms // 1000  # bytes per 80ms
        for i in range(0, len(sip_audio), browser_chunk_size):
            await self.send(bytes_data=sip_audio[i:i + browser_chunk_size])
            await asyncio.sleep(0.005)

    # ── Receive loop ──────────────────────────────────────────────────

    async def _receive_loop(self, session):
        greeting_buffer = bytearray()
        self._pending_tool_calls = 0
        try:
            while not self._disconnecting:
                async for response in session.receive():
                    sc = getattr(response, "server_content", None)
                    tc = getattr(response, "tool_call", None)

                    self._track_usage_metadata(response)

                    if getattr(response, "session_resumption_update", None):
                        update = response.session_resumption_update
                        if update.resumable and update.new_handle:
                            self._last_session_handle = update.new_handle

                    if getattr(response, "go_away", None):
                        self._schedule_auto_close(1.0, "gemini_go_away")
                        return

                    if tc:
                        await self._handle_tool_call(session, tc, greeting_buffer)
                        continue

                    if sc is None:
                        continue

                    self._handle_input_transcript(sc)
                    self._handle_output_transcript(sc)
                    await self._handle_model_turn(sc, session, greeting_buffer)
                    await self._handle_turn_complete(sc, session, greeting_buffer)

        except ConnectionClosed as exc:
            logger.info("Gemini receive loop closed: code=%s", getattr(exc, "code", None))
            self._clear_session_state()

    def _track_usage_metadata(self, response):
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        m = self._usage_metrics
        m["prompt"]   = max(m["prompt"],   getattr(usage, "prompt_token_count", 0) or 0)
        m["response"] = max(m["response"], getattr(usage, "response_token_count", 0) or 0)
        m["total"]    = max(m["total"],    getattr(usage, "total_token_count", 0) or 0)

        thoughts = getattr(usage, "thoughts_token_count", 0) or 0
        if thoughts > 0:
            self._thinking_tokens_from_usage = max(getattr(self, "_thinking_tokens_from_usage", 0), thoughts)

        for detail in (getattr(usage, "prompt_tokens_details", None) or []):
            modality = str(getattr(detail, "modality", "")).upper()
            tokens = getattr(detail, "token_count", 0) or 0
            if "TEXT" in modality:  m["input_text"]  = max(m["input_text"], tokens)
            elif "AUDIO" in modality: m["input_audio"] = max(m["input_audio"], tokens)

        for detail in (getattr(usage, "response_tokens_details", None) or []):
            modality = str(getattr(detail, "modality", "")).upper()
            tokens = getattr(detail, "token_count", 0) or 0
            if "TEXT" in modality:  m["output_text"]  = max(m["output_text"], tokens)
            elif "AUDIO" in modality: m["output_audio"] = max(m["output_audio"], tokens)

    def _handle_input_transcript(self, sc):
        t = getattr(sc, "input_transcription", None)
        if t and hasattr(t, "text") and getattr(t, "text", None):
            if self._record_user_transcript(t.text):
                self._mark_user_input()
                self._cancel_silence_check()

    def _handle_output_transcript(self, sc):
        t = getattr(sc, "output_transcription", None)
        if t and hasattr(t, "text") and t.text:
            self._current_agent_turn += t.text

    async def _handle_model_turn(self, sc, session, greeting_buffer):
        model_turn = getattr(sc, "model_turn", None)
        if model_turn is None:
            return
        for part in model_turn.parts:
            if getattr(part, "text", None) and not self._current_agent_turn.endswith(part.text):
                self._current_agent_turn += part.text
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                self._agent_speaking = True
                if self._save_as_greeting:
                    greeting_buffer.extend(inline.data)
                await self._stream_pcm_to_sip(inline.data)

    async def _handle_turn_complete(self, sc, session, greeting_buffer):
        if not (getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False)):
            return

        self._agent_speaking = False
        if self._save_as_greeting and greeting_buffer:
            save_wav(bytes(greeting_buffer), self._get_greeting_path(), OUT_RATE)
            self._save_as_greeting = False
            greeting_buffer.clear()

        if not self._current_agent_turn:
            return

        self._call_history.append({"role": "agent", "text": self._current_agent_turn.strip()})
        idx = self._current_agent_turn.lower()

        goodbye_detected = any(p in idx for p in ["allah hafiz", "khuda hafiz", "goodbye", "bye"])
        terminal_tool_called = any(
            h.get("tool_name") in ("book_appointment", "place_order")
            and not (isinstance(h.get("tool_result"), dict) and h["tool_result"].get("error"))
            for h in self._call_history
        )
        order_filler_phrases = ["order laga", "placing your order", "placing the order", "order place", "order enter kar", "order system mein", "main aap ka order"]
        agent_spoke_filler = any(
            any(p in h.get("text", "").lower() for p in order_filler_phrases)
            for h in self._call_history if h.get("role") == "agent"
        )
        order_in_progress = getattr(self, "_booking_state", None) in ("booking_requested", "confirmed")

        if goodbye_detected:
            if (order_in_progress or agent_spoke_filler) and not terminal_tool_called:
                self._should_end_call = False
                try:
                    await session.send_realtime_input(text="[System: CRITICAL — You said goodbye but did NOT call the place_order tool. Call it RIGHT NOW.]")
                except Exception:
                    pass
            else:
                self._should_end_call = True
        else:
            self._schedule_silence_check()

        if self._should_end_call:
            asyncio.create_task(self._delayed_close(6.0))
        self._current_agent_turn = ""

    async def _handle_tool_call(self, session, tool_call, greeting_buffer):
        self._cancel_silence_check()
        self._pending_tool_calls += len(tool_call.function_calls)
        function_responses = []
        for fc in tool_call.function_calls:
            tool_name = fc.name
            tool_args = dict(fc.args) if fc.args else {}
            validation_error = self._validate_terminal_tool_args(tool_name, tool_args)
            result = validation_error if validation_error else await self._execute_tool(tool_name, tool_args)

            self._call_history.append({"role": "tool", "tool_name": tool_name, "tool_args": tool_args, "tool_result": result})
            function_responses.append(types.FunctionResponse(name=tool_name, id=fc.id, response={"result": result}))

        try:
            await session.send_tool_response(function_responses=function_responses)
            self._pending_tool_calls = 0
            if self._audio_queue:
                try:
                    await session.send_realtime_input(audio=types.Blob(data=bytes(self._audio_queue), mime_type=f"audio/pcm;rate={MIC_RATE}"))
                except Exception:
                    pass
                self._audio_queue.clear()
        except Exception as e:
            self._pending_tool_calls = 0
            import traceback
            traceback.print_exc()
