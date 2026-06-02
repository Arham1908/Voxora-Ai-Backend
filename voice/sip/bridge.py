import asyncio
import audioop
import json
import logging
import os
import threading
import time
import uuid
import wave
from pathlib import Path
from pyVoIP.VoIP import InvalidStateError, CallState

from ._constants import SIP_RATE, MIC_RATE, OUT_RATE, FRAME_DURATION

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError("pip install google-genai")


class SIPCallBridge:
    def __init__(self, call, agent_id="healthcare", voice="Aoede", language="ur-PK"):
        self.call     = call
        self.agent_id = agent_id
        self.voice    = voice
        self.language = language
        self._session_uuid   = str(uuid.uuid4())
        self._running        = False
        self._gemini_session = None
        self._loop           = None
        self._upsample_state   = None
        self._downsample_state = None
        self._start_time    = time.time()
        self._usage_metrics = {"prompt": 0, "response": 0, "total": 0, "input_text": 0, "input_audio": 0, "output_text": 0, "output_audio": 0}
        self._call_history       = []
        self._current_agent_turn = ""
        logger.info("[Bridge %s] Created: agent=%s voice=%s lang=%s", self._session_uuid[:8], agent_id, voice, language)

    def start(self):
        self._running = True
        t = threading.Thread(target=self._run_async_loop, daemon=True, name=f"Bridge-{self._session_uuid[:8]}")
        t.start()

    def _run_async_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_gemini_session())
        except Exception as e:
            logger.error("[Bridge %s] Fatal: %s", self._session_uuid[:8], e, exc_info=True)
        finally:
            self._running = False
            self._loop.close()

    async def _run_gemini_session(self):
        from .agents.registry import get_agent
        agent_cfg = get_agent(self.agent_id)
        if not agent_cfg:
            logger.error("[Bridge %s] Unknown agent: %s", self._session_uuid[:8], self.agent_id)
            return
        schedule_data = await self._fetch_schedule_data()
        greeting_path_fn = agent_cfg.get("greeting_path_fn")
        greeting_path = greeting_path_fn(self.language, self.voice) if greeting_path_fn else agent_cfg["greeting_path"]
        has_cached_greeting = greeting_path.exists()
        system_prompt = agent_cfg["build_system_prompt"](language=self.language, voice=self.voice, has_cached_greeting=has_cached_greeting, schedule_data=schedule_data)
        tools = agent_cfg["tools_fn"]()
        self._execute_tool_fn = agent_cfg["execute_tool"]
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.error("[Bridge %s] GEMINI_API_KEY not set!", self._session_uuid[:8])
            return
        client = genai.Client(api_key=api_key)
        live_config = types.LiveConnectConfig(
            system_instruction=types.Content(parts=[types.Part(text=system_prompt)]),
            response_modalities=["AUDIO"],
            tools=tools,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)),
                language_code=self.language,
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                )
            ),
        )
        logger.info("[Bridge %s] Connecting to Gemini Live...", self._session_uuid[:8])
        t0 = time.time()
        try:
            async with client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=live_config) as session:
                self._gemini_session = session
                await self._handle_greeting(session, agent_cfg, greeting_path, has_cached_greeting)
                tasks = [
                    asyncio.create_task(self._sip_to_gemini(session)),
                    asyncio.create_task(self._gemini_to_sip(session, agent_cfg)),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending: t.cancel()
        except Exception as e:
            logger.error("[Bridge %s] Gemini session error: %s", self._session_uuid[:8], e, exc_info=True)
        finally:
            self._gemini_session = None
            await self._save_session_cost()
            self._cleanup_call()

    async def _fetch_schedule_data(self):
        try:
            from appointment.models import Schedule
            from appointment.serializers import ScheduleSerializer
            from asgiref.sync import sync_to_async
            schedules = await sync_to_async(lambda: list(Schedule.objects.all()))()
            return ScheduleSerializer(schedules, many=True).data
        except Exception as e:
            logger.warning("[Bridge %s] Schedule fetch failed: %s", self._session_uuid[:8], e)
            return []

    async def _handle_greeting(self, session, agent_cfg, greeting_path, has_cached):
        if has_cached:
            pcm = self._load_wav_pcm(greeting_path)
            self._write_pcm24k_to_sip(pcm)
        else:
            gen_fn = agent_cfg.get("generate_greeting_prompt_fn")
            prompt = gen_fn(self.language, self.voice) if gen_fn else agent_cfg.get("greeting_prompt", "Greet warmly.")
            self._save_as_greeting   = True
            self._greeting_buffer    = bytearray()
            self._greeting_save_path = greeting_path
            await session.send_realtime_input(text=prompt)

    def _load_wav_pcm(self, path):
        with wave.open(str(path), "rb") as wf:
            return wf.readframes(wf.getnframes())

    def _write_pcm24k_to_sip(self, pcm_24k):
        try:
            pcm_8k, self._downsample_state = audioop.ratecv(pcm_24k, 2, 1, OUT_RATE, SIP_RATE, self._downsample_state)
            ulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
            for i in range(0, len(ulaw_8k), 160):
                if not self._running: break
                try:
                    self.call.write_audio(ulaw_8k[i:i + 160])
                except (InvalidStateError, OSError):
                    self._running = False
                    break
                time.sleep(FRAME_DURATION)
        except Exception as e:
            logger.error("[Bridge %s] Audio write error: %s", self._session_uuid[:8], e)

    async def _sip_to_gemini(self, session):
        frames = 0
        try:
            while self._running:
                try:
                    if self.call.state != CallState.ANSWERED: break
                except Exception: break
                try:
                    ulaw = await asyncio.get_event_loop().run_in_executor(None, self._read_sip_audio)
                except Exception: break
                if not ulaw:
                    await asyncio.sleep(0.01)
                    continue
                pcm_8k  = audioop.ulaw2lin(ulaw, 2)
                pcm_16k, self._upsample_state = audioop.ratecv(pcm_8k, 2, 1, SIP_RATE, MIC_RATE, self._upsample_state)
                try:
                    await session.send_realtime_input(audio=types.Blob(data=pcm_16k, mime_type=f"audio/pcm;rate={MIC_RATE}"))
                    frames += 1
                except Exception as e:
                    logger.error("[Bridge %s] Gemini send error: %s", self._session_uuid[:8], e)
                    break
        except asyncio.CancelledError: pass
        finally:
            if not self._running: pass
            logger.info("[Bridge %s] SIP->Gemini loop ended (%d frames)", self._session_uuid[:8], frames)

    def _read_sip_audio(self):
        try:
            data = self.call.read_audio(length=160, blocking=True)
            return data if data else b""
        except InvalidStateError:
            self._running = False
            return b""
        except Exception: return b""

    async def _gemini_to_sip(self, session, agent_cfg):
        greeting_buffer  = bytearray()
        save_as_greeting = getattr(self, "_save_as_greeting", False)
        try:
            while self._running:
                async for response in session.receive():
                    if not self._running: break
                    usage = getattr(response, "usage_metadata", None)
                    if usage:
                        for attr, key in [("prompt_token_count", "prompt"), ("response_token_count", "response"), ("total_token_count", "total")]:
                            val = getattr(usage, attr, 0) or 0
                            self._usage_metrics[key] = max(self._usage_metrics[key], val)
                    tool_call = getattr(response, "tool_call", None)
                    if tool_call:
                        fn_responses = []
                        for fc in tool_call.function_calls:
                            args = dict(fc.args) if fc.args else {}
                            result = await self._execute_tool_fn(fc.name, args)
                            self._call_history.append({"role": "tool", "tool_name": fc.name, "tool_args": args, "tool_result": result})
                            fn_responses.append(types.FunctionResponse(name=fc.name, id=fc.id, response={"result": result}))
                        try:
                            await session.send_tool_response(function_responses=fn_responses)
                        except Exception as e:
                            logger.error("[Bridge %s] Tool response error: %s", self._session_uuid[:8], e)
                        continue
                    sc = getattr(response, "server_content", None)
                    if sc is None: continue
                    if getattr(sc, "input_transcription", None):
                        t = sc.input_transcription
                        if hasattr(t, "text") and t.text:
                            self._call_history.append({"role": "user", "text": t.text})
                    if getattr(sc, "output_transcription", None):
                        t = sc.output_transcription
                        if hasattr(t, "text") and t.text:
                            if not self._current_agent_turn.endswith(t.text):
                                self._current_agent_turn += t.text
                    if getattr(sc, "model_turn", None):
                        for part in sc.model_turn.parts:
                            if getattr(part, "text", None):
                                if not self._current_agent_turn.endswith(part.text):
                                    self._current_agent_turn += part.text
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                if save_as_greeting: greeting_buffer.extend(inline.data)
                                await asyncio.get_event_loop().run_in_executor(None, self._write_pcm24k_to_sip, inline.data)
                    if getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False):
                        if save_as_greeting and greeting_buffer:
                            self._save_wav(bytes(greeting_buffer), getattr(self, "_greeting_save_path", None))
                            save_as_greeting = self._save_as_greeting = False
                            greeting_buffer.clear()
                        if self._current_agent_turn:
                            self._call_history.append({"role": "agent", "text": self._current_agent_turn.strip()})
                            idx = self._current_agent_turn.lower()
                            if any(p in idx for p in ["allah hafiz", "khuda hafiz", "goodbye", "bye"]):
                                await asyncio.sleep(5)
                                self._running = False
                                break
                            self._current_agent_turn = ""
        except asyncio.CancelledError: pass
        except Exception as e:
            logger.error("[Bridge %s] Receive error: %s", self._session_uuid[:8], e, exc_info=True)
        finally:
            self._running = False

    def _save_wav(self, pcm, path):
        if not path: return
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(OUT_RATE)
                wf.writeframes(pcm)
        except Exception as e:
            logger.error("[Bridge %s] WAV save error: %s", self._session_uuid[:8], e)

    def _cleanup_call(self):
        try:
            if self.call.state == CallState.ANSWERED:
                self.call.hangup()
        except Exception as e:
            logger.debug("[Bridge %s] Hangup error: %s", self._session_uuid[:8], e)

    async def _save_session_cost(self):
        duration = int(time.time() - self._start_time)
        m = self._usage_metrics
        if m["total"] > 0 or duration > 0:
            try:
                from asgiref.sync import sync_to_async
                from Analytics.models import GeminiSessionCost
                total_cost = (m["input_text"] * 0.00000075 + m["input_audio"] * 0.000003 + m["output_text"] * 0.0000045 + m["output_audio"] * 0.000012)
                await sync_to_async(GeminiSessionCost.objects.create)(
                    session_id=self._session_uuid, agent_type=self.agent_id,
                    prompt_tokens=m["prompt"], response_tokens=m["response"], total_tokens=m["total"],
                    input_text_tokens=m["input_text"], input_audio_tokens=m["input_audio"],
                    output_text_tokens=m["output_text"], output_audio_tokens=m["output_audio"],
                    call_duration_seconds=duration, estimated_cost_usd=total_cost,
                )
            except Exception as e:
                logger.error("[Bridge %s] Cost save failed: %s", self._session_uuid[:8], e)
        if self._call_history:
            try:
                from asgiref.sync import sync_to_async
                from Analytics.models import CallHistory
                await sync_to_async(CallHistory.objects.create)(
                    session_id=self._session_uuid, agent_type=self.agent_id,
                    duration_seconds=duration, transcript=self._call_history,
                )
            except Exception as e:
                logger.error("[Bridge %s] History save failed: %s", self._session_uuid[:8], e)
