# voice/consumers_browser.py
#
# Browser WebSocket variant — dynamic multi-agent, raw PCM16 in/out.
# Agent, voice, and language are resolved from:
#   - URL path: /ws/voice/<agent_id>/
#   - Query string: ?voice=Aoede&language=ur-PK

from pathlib import Path
from google.genai import types
from websockets.exceptions import ConnectionClosed
import asyncio
import json
import urllib.parse
import truststore
truststore.inject_into_ssl()

from ..audio.utils import MIC_RATE, OUT_RATE, clean_transcript_text, save_wav
from .base import VoiceAgentConsumer
from ..agents.registry import get_agent

BROWSER_PCM_CHUNK = 4800  # ~100ms at 24kHz PCM16
HEARTBEAT_INTERVAL = 20   # seconds between keep-alive pings to mobile clients


def _parse_query(scope) -> dict:
    """Parse ?key=value pairs from the WebSocket scope query string."""
    qs = scope.get("query_string", b"").decode("utf-8")
    return dict(urllib.parse.parse_qsl(qs))


class BrowserVoiceConsumer(VoiceAgentConsumer):
    """
    One instance per browser WebSocket connection.
    Resolves agent config from URL path + query params at connect time.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._agent_cfg = None
        self._voice = "Aoede"
        self._language = "ur-PK"
        self._heartbeat_task = None
        self._diag_frame_count = 0       # Total audio frames received
        self._diag_bytes_total = 0       # Total audio bytes received
        self._diag_text_count = 0        # Total text messages received
        self._diag_user_agent = ""       # Captured at connect time

    # ------------------------------------------------------------------
    # WebSocket lifecycle — resolve agent config first
    # ------------------------------------------------------------------

    async def connect(self):
        # ── DIAGNOSTIC: Capture User-Agent and connection metadata ──
        headers = dict(self.scope.get("headers", []))
        self._diag_user_agent = headers.get(b"user-agent", b"").decode(errors="replace")
        origin = headers.get(b"origin", b"").decode(errors="replace")
        print(f"   📱 User-Agent: {self._diag_user_agent}\n   🌐 Origin: {origin}\n   🔗 Path: {self.scope.get('path', '?')}", flush=True)

        # Resolve agent_id from URL kwargs (set by routing.py)
        agent_id = self.scope["url_route"]["kwargs"].get("agent_id", "healthcare")
        self._agent_cfg = get_agent(agent_id)

        if self._agent_cfg is None:
            print(f"[BrowserWS] Unknown agent_id='{agent_id}', closing.", flush=True)
            await self.close(code=4004)
            return

        # Resolve voice and language from query string, with per-agent defaults
        params = _parse_query(self.scope)
        self._voice = params.get("voice", self._agent_cfg["default_voice"])
        self._language = params.get("language", self._agent_cfg["default_language"])

        print(
            f"   🤖 Agent='{agent_id}' Voice='{self._voice}' Language='{self._language}'",
            flush=True,
        )

        # Delegate to parent (creates Gemini client, starts session task)
        await super().connect()

        # Start keep-alive heartbeat to prevent mobile browsers from killing the WS
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._tasks.append(self._heartbeat_task)

    # ------------------------------------------------------------------
    # Override: send session_ready JSON to browser after Gemini connects
    # ------------------------------------------------------------------

    async def _on_gemini_ready(self):
        """Called by parent after Gemini Live session opens.

        We send ONLY the audio config metadata here (sample rates, agent info)
        so the browser can configure its AudioContext.  We deliberately do NOT
        send 'session_ready' at this point — that signal is sent by the parent
        (_run_gemini_session) AFTER the greeting has finished playing, so the
        browser mic gate opens only when the user is actually expected to speak.
        """
        try:
            msg = json.dumps({
                "event": "audio_config",   # <-- renamed from session_ready
                "output_sample_rate": OUT_RATE,
                "input_sample_rate": MIC_RATE,
                "agent_id": self._agent_cfg["id"],
                "agent_name": self._agent_cfg["name"],
                "voice": self._voice,
                "language": self._language,
            })
            await self.send(text_data=msg)
            print("[BrowserWS] Sent audio_config to browser (session_ready will follow after greeting)", flush=True)
        except Exception as e:
            print(f"[BrowserWS] ERROR Failed to send audio_config: {e}", flush=True)

    # ------------------------------------------------------------------
    # Override: expose dynamic config to parent _run_gemini_session
    # ------------------------------------------------------------------

    def _get_agent_key(self) -> str:
        return self._agent_cfg["id"] if self._agent_cfg else "default"

    def _get_system_prompt(self, has_cached_greeting: bool = False, schedule_data: list = None) -> str:
        return self._agent_cfg["build_system_prompt"](
            language=self._language,
            voice=self._voice,
            has_cached_greeting=has_cached_greeting,
            schedule_data=schedule_data,
        )

    def _get_tools(self):
        return self._agent_cfg["tools_fn"]()

    def _get_voice_name(self) -> str:
        return self._voice

    def _get_language_code(self) -> str:
        return self._language

    def _get_greeting_path(self) -> Path:
        # Use language+voice-aware greeting path if available
        fn = self._agent_cfg.get("greeting_path_fn")
        if fn:
            return fn(self._language, self._voice)
        return self._agent_cfg["greeting_path"]

    def _get_greeting_prompt(self) -> str:
        # Use language-aware greeting prompt if available
        fn = self._agent_cfg.get("greeting_prompt_fn")
        if fn:
            return fn(self._language)
        return self._agent_cfg["greeting_prompt"]

    def _get_generate_greeting_prompt(self) -> str:
        """Prompt used when NO cached greeting exists — model must greet the user."""
        fn = self._agent_cfg.get("generate_greeting_prompt_fn")
        if fn:
            return fn(self._language, self._voice)
        # Fallback: use the regular greeting prompt (backward compat)
        return self._get_greeting_prompt()

    async def _execute_tool(self, tool_name: str, tool_args: dict) -> dict:
        """Delegate tool execution to the active agent's executor."""
        if tool_name == "menu":
            if getattr(self, "_cached_menu", None):
                print(f"[BrowserWS] ⚡ Returning cached menu for duplicate tool call.", flush=True)
                return self._cached_menu
                
            result = await self._agent_cfg["execute_tool"](tool_name, tool_args)
            if isinstance(result, dict) and result.get("success"):
                self._cached_menu = result
            return result
            
        return await self._agent_cfg["execute_tool"](tool_name, tool_args)

    # ------------------------------------------------------------------
    # Override: receive raw PCM16 from browser, or use parent for Twilio
    # ------------------------------------------------------------------

    async def receive(self, bytes_data=None, text_data=None):
        # ── DIAGNOSTIC: Log EVERYTHING entering receive() ──
        if text_data:
            self._diag_text_count += 1
            print(
                f"📥 [BrowserWS] RECEIVE text #{self._diag_text_count}: "
                f"{text_data[:200] if text_data else None}",
                flush=True,
            )

        # Log first 5 frames, then every 100th
        if bytes_data:
            self._diag_frame_count += 1
            self._diag_bytes_total += len(bytes_data)
            if self._diag_frame_count <= 5 or self._diag_frame_count % 100 == 0:
                print(
                    f"📥 [BrowserWS] RECEIVE audio frame #{self._diag_frame_count}: "
                    f"len={len(bytes_data)} bytes, type={type(bytes_data).__name__}, "
                    f"total_bytes_so_far={self._diag_bytes_total}, "
                    f"session_ready={self._session_ready.is_set()}, "
                    f"disconnecting={self._disconnecting}, "
                    f"gemini_session={'alive' if self.gemini_session else 'None'}, "
                    f"pending_tools={self._pending_tool_calls}, "
                    f"UA_short={'iPhone' if 'iPhone' in self._diag_user_agent else 'Android' if 'Android' in self._diag_user_agent else 'Desktop'}",
                    flush=True,
                )

        if not bytes_data and not text_data:
            print(f"📥 [BrowserWS] RECEIVE called with NOTHING (bytes=None, text=None)", flush=True)
            return

        if self._transport == "twilio":
            return await super().receive(bytes_data=bytes_data, text_data=text_data)

        if self._disconnecting or not bytes_data:
            if self._disconnecting and bytes_data:
                print(f"⚠️ [BrowserWS] Dropping audio — disconnecting", flush=True)
            return
        if len(bytes_data) % 2 != 0:
            print(f"⚠️ [BrowserWS] Dropping odd-length frame: {len(bytes_data)} bytes", flush=True)
            return
        if not self._session_ready.is_set():
            if self._diag_frame_count <= 5:
                print(f"⚠️ [BrowserWS] Dropping frame — session NOT ready yet", flush=True)
            return

        session = self.gemini_session
        if session is None:
            print(f"⚠️ [BrowserWS] Dropping frame — gemini_session is None", flush=True)
            self._clear_session_state()
            return

        # Debug: accumulate mic audio for WAV dump on disconnect
        if not hasattr(self, '_debug_mic_buffer'):
            self._debug_mic_buffer = bytearray()
        self._debug_mic_buffer.extend(bytes_data)

        # Agent State / Tool Lock: buffer incoming audio while a tool is executing
        if self._pending_tool_calls > 0:
            if self._audio_queue is None:
                self._audio_queue = bytearray()
            self._audio_queue.extend(bytes_data)
            return

        try:
            if not hasattr(self, '_recv_count'):
                self._recv_count = 0
            self._recv_count += 1

            await session.send_realtime_input(
                audio=types.Blob(
                    data=bytes_data,
                    mime_type=f"audio/pcm;rate={MIC_RATE}",
                )
            )
        except ConnectionClosed as exc:
            print(f"❌ [BrowserWS] ConnectionClosed while forwarding audio: {exc}", flush=True)
            self._clear_session_state()
        except Exception as e:
            print(f">>> [BrowserWS] Error forwarding audio to Gemini: {e}", flush=True)

    async def disconnect(self, close_code):
        # ── DIAGNOSTIC: Summarize the entire session on disconnect ──
        _CLOSE_CODES = {
            1000: "Normal close",
            1001: "Browser tab closed / navigated away",
            1006: "Abnormal — network drop or timeout",
            1009: "Message too large",
            1011: "Server error",
            4004: "Unknown agent_id",
        }
        code_meaning = _CLOSE_CODES.get(close_code, "Unknown")
        ua_tag = (
            "iPhone" if "iPhone" in self._diag_user_agent
            else "Android" if "Android" in self._diag_user_agent
            else "Desktop"
        )
        print(
            f"❌ [BrowserWS] DISCONNECTED\n"
            f"   📱 Device: {ua_tag}\n"
            f"   🔢 Close code: {close_code} ({code_meaning})\n"
            f"   🎤 Total audio frames received: {self._diag_frame_count}\n"
            f"   📦 Total audio bytes received: {self._diag_bytes_total}\n"
            f"   💬 Total text messages received: {self._diag_text_count}\n"
            f"   📱 Full UA: {self._diag_user_agent[:120]}",
            flush=True,
        )

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if hasattr(self, '_debug_mic_buffer') and len(self._debug_mic_buffer) > 0:
            from django.conf import settings
            debug_path = settings.BASE_DIR / "media/debug_mic.wav"
            save_wav(bytes(self._debug_mic_buffer), debug_path, MIC_RATE)
        await super().disconnect(close_code)

    # ------------------------------------------------------------------
    # Heartbeat — keep mobile WebSocket connections alive
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self):
        """Send a lightweight JSON ping every HEARTBEAT_INTERVAL seconds.

        Mobile browsers (iOS Safari, Android Chrome) will silently kill WebSocket
        connections that appear idle. A regular ping prevents this and also lets
        the backend detect dead connections faster than the TCP timeout would.
        """
        try:
            while not self._disconnecting:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._disconnecting:
                    break
                try:
                    await self.send(text_data=json.dumps({"type": "ping"}))
                except Exception:
                    # Connection is already gone — stop the loop cleanly
                    break
        except asyncio.CancelledError:
            pass


    # ------------------------------------------------------------------
    # Override: stream raw PCM16 to browser or defer to parent for Twilio
    # ------------------------------------------------------------------

    async def _stream_pcm_to_sip(self, pcm_24k: bytes):
        """Stream cached greeting PCM directly to browser in chunks, or via SIP."""
        if self._transport == "twilio":
            return await super()._stream_pcm_to_sip(pcm_24k)

        print(f"[BrowserWS] Streaming cached greeting ({len(pcm_24k)} bytes)", flush=True)
        try:
            for i in range(0, len(pcm_24k), BROWSER_PCM_CHUNK):
                await self.send(bytes_data=pcm_24k[i: i + BROWSER_PCM_CHUNK])
                await asyncio.sleep(0.1)
            print("[BrowserWS] Finished streaming greeting", flush=True)
        except Exception as e:
            print(f">>> [BrowserWS] Error during _stream_pcm_to_sip: {e}", flush=True)
            raise

    # ------------------------------------------------------------------
    # Override: receive loop — sends raw PCM16 and handles tool calls
    # ------------------------------------------------------------------

    async def _receive_loop(self, session):
        greeting_buffer = bytearray()
        greeting_path = self._get_greeting_path()
        self._pending_tool_calls = 0  # Ensure initialized even if parent hasn't set it

        try:
            while not self._disconnecting:
                async for response in session.receive():
                    sc = getattr(response, "server_content", None)
                    tc = getattr(response, "tool_call", None)

                    # ── Track usage metrics for cost calculation ───────────
                    usage = getattr(response, "usage_metadata", None)
                    if usage:
                        self._usage_metrics["prompt"] = max(self._usage_metrics["prompt"], getattr(usage, "prompt_token_count", 0) or 0)
                        self._usage_metrics["response"] = max(self._usage_metrics["response"], getattr(usage, "response_token_count", 0) or 0)
                        self._usage_metrics["total"] = max(self._usage_metrics["total"], getattr(usage, "total_token_count", 0) or 0)

                        prompt_details = getattr(usage, "prompt_tokens_details", None) or []
                        for detail in prompt_details:
                            modality = getattr(detail, "modality", None)
                            token_count = getattr(detail, "token_count", 0) or 0
                            modality_str = str(modality).upper() if modality else ""
                            if "TEXT" in modality_str:
                                self._usage_metrics["input_text"] = max(self._usage_metrics["input_text"], token_count)
                            elif "AUDIO" in modality_str:
                                self._usage_metrics["input_audio"] = max(self._usage_metrics["input_audio"], token_count)

                        response_details = getattr(usage, "response_tokens_details", None) or []
                        for detail in response_details:
                            modality = getattr(detail, "modality", None)
                            token_count = getattr(detail, "token_count", 0) or 0
                            modality_str = str(modality).upper() if modality else ""
                            if "TEXT" in modality_str:
                                self._usage_metrics["output_text"] = max(self._usage_metrics["output_text"], token_count)
                            elif "AUDIO" in modality_str:
                                self._usage_metrics["output_audio"] = max(self._usage_metrics["output_audio"], token_count)

                        if not getattr(self, "_logged_modality_sample", False) and self._usage_metrics["total"] > 0:
                            self._logged_modality_sample = True
                            print(f"[BrowserWS DEBUG] prompt_tokens_details: {prompt_details}", flush=True)
                            print(f"[BrowserWS DEBUG] response_tokens_details: {response_details}", flush=True)

                    if sc and getattr(sc, "model_turn", None):
                        for p in sc.model_turn.parts:
                            if getattr(p, "text", None):
                                print(f"[BrowserWS DEBUG] Model Text: {p.text}", flush=True)

                    if getattr(response, "go_away", None):
                        go_away = response.go_away
                        print(
                            f"[BrowserWS] Received GoAway from Gemini. time_left={go_away.time_left}s — auto-closing.",
                            flush=True,
                        )
                        self._schedule_auto_close(1.0, "gemini_go_away")
                        return

                    tool_call = getattr(response, "tool_call", None)
                    if tool_call:
                        self._pending_tool_calls += len(tool_call.function_calls)
                        terminal_tool_completed = False
                        # Cancel any pending silence timer — we're entering tool processing
                        self._cancel_silence_check()
                        # Flush any partial audio the browser has already buffered
                        # (e.g. "Let me check..." spoken before the tool fires).
                        # Without this the user hears: partial snippet → pause → full answer.
                        try:
                            await self.send(text_data=json.dumps({"event": "clear"}))
                            print("[BrowserWS] Sent clear to browser (flushing pre-tool audio)", flush=True)
                        except Exception:
                            pass
                        # Before tool call, save any pending agent turn to history
                        if self._current_agent_turn:
                            self._call_history.append({"role": "agent", "text": self._current_agent_turn})
                            self._current_agent_turn = ""

                        function_responses = []
                        for fc in tool_call.function_calls:
                            tool_name = fc.name
                            tool_args = dict(fc.args) if fc.args else {}
                            print(f"[BrowserWS] [Tool Call] {tool_name}({tool_args})", flush=True)

                            if tool_name in ("book_appointment", "place_order"):
                                # ── Duplicate terminal tool guard ───────────────
                                # Silence nudges can cause Gemini to re-confirm and
                                # re-call the terminal tool. Block the second execution.
                                if self._booking_state == "booked":
                                    print(f"[BrowserWS] ⛔ Duplicate {tool_name} blocked — already booked!", flush=True)
                                    result = {"error": "This order/appointment has already been placed. Do NOT call this tool again."}
                                    self._call_history.append({
                                        "role": "tool",
                                        "tool_name": tool_name,
                                        "tool_args": tool_args,
                                        "tool_result": result,
                                    })
                                    function_responses.append(
                                        types.FunctionResponse(
                                            name=tool_name,
                                            id=fc.id,
                                            response={"result": result},
                                        )
                                    )
                                    continue  # skip to next fc
                                self._booking_state = "booking_requested"
                                print(f"[BrowserWS] {tool_name} tool requested", flush=True)

                            validation_error = self._validate_terminal_tool_args(tool_name, tool_args)
                            if validation_error:
                                result = validation_error
                            else:
                                result = await self._execute_tool(tool_name, tool_args)
                            print(f"[BrowserWS] [Tool Result] {tool_name} → {result}", flush=True)
                            if (
                                tool_name in ("book_appointment", "place_order")
                                and not (isinstance(result, dict) and result.get("error"))
                            ):
                                self._booking_state = "booked"
                                terminal_tool_completed = True
                            
                            self._call_history.append({
                                "role": "tool",
                                "tool_name": tool_name,
                                "tool_args": tool_args,
                                "tool_result": result,
                            })

                            function_responses.append(
                                types.FunctionResponse(
                                    name=tool_name,
                                    id=fc.id,
                                    response={"result": result},
                                )
                            )

                        if not function_responses:
                            # All calls were blocked duplicates — nothing to send
                            self._pending_tool_calls = 0
                            continue

                        try:
                            await session.send_tool_response(function_responses=function_responses)
                            self._pending_tool_calls = 0  # Tool calls completed
                            print(f"[BrowserWS] Successfully sent tool responses for {len(function_responses)} calls", flush=True)
                            
                            # Discard audio queued during tool execution — these frames
                            # are mostly the agent's own echo captured by the mic (since
                            # the speaker was playing filler audio). Flushing them into
                            # Gemini right as it starts speaking the tool result creates
                            # a burst of "user speech" that confuses VAD.
                            if hasattr(self, '_audio_queue') and self._audio_queue:
                                print(f"[BrowserWS] Discarding {len(self._audio_queue)} bytes of stale queued audio (tool boundary)", flush=True)
                                self._audio_queue.clear()
                            
                            if terminal_tool_completed:
                                print("[BrowserWS] Terminal tool succeeded — auto-close armed.", flush=True)
                                self._schedule_auto_close(10.0, "terminal_tool_completed")
                            # NOTE: Do NOT schedule silence check here — Gemini is about to
                            # speak the verbal response. We set _agent_speaking=True when audio
                            # arrives and schedule silence only after turn_complete.
                        except Exception as e:
                            self._pending_tool_calls = 0
                            print(f">>> [BrowserWS ERROR] Failed to send tool response: {repr(e)}", flush=True)
                        # Continue to wait for Gemini's response after tool results
                        continue

                    # ── Audio + transcription handling ──────────────────────
                    sc = getattr(response, "server_content", None)
                    if sc is None:
                        continue

                    # Track user transcription for call history
                    if getattr(sc, "input_transcription", None):
                        t = sc.input_transcription
                        if hasattr(t, "text") and t.text:
                            if self._record_user_transcript(t.text):
                                print(f"[BrowserWS] [User] {clean_transcript_text(t.text)}", flush=True)
                                self._mark_user_input()  # Record actual user speech time
                                self._cancel_silence_check()  # Cancel pending timer

                    # Track model transcription (voice) for high-fidelity audio history
                    if getattr(sc, "output_transcription", None):
                        t = sc.output_transcription
                        if hasattr(t, "text") and t.text:
                            if not self._current_agent_turn.endswith(t.text):
                                self._current_agent_turn += t.text

                    if getattr(sc, "model_turn", None):
                        for part in sc.model_turn.parts:
                            # Also track direct text parts (though rarer in Voice mode)
                            if getattr(part, "text", None):
                                if not self._current_agent_turn.endswith(part.text):
                                    self._current_agent_turn += part.text

                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                # Signal frontend to mute mic when agent starts speaking
                                # so the mic doesn't capture the agent's own audio output.
                                if not self._agent_speaking:
                                    self._agent_speaking = True
                                    try:
                                        await self.send(text_data=json.dumps({"type": "agent_speaking", "value": True}))
                                    except Exception:
                                        pass
                                if self._save_as_greeting:
                                    greeting_buffer.extend(inline.data)
                                if self._disconnecting:
                                    # WebSocket is closing — silently drop outbound audio
                                    continue
                                if self._transport == "twilio":
                                    await self._stream_pcm_to_sip(inline.data)
                                else:
                                    try:
                                        await self.send(bytes_data=inline.data)
                                    except Exception as send_exc:
                                        # Daphne raises Disconnected when the WS
                                        # protocol is already torn down (e.g. user
                                        # navigated away or silence auto-close fired).
                                        print(
                                            f"❌ [BrowserWS] ConnectionClosed while forwarding audio: {send_exc}",
                                            flush=True,
                                        )
                                        self._clear_session_state()
                                        return  # Exit the receive loop cleanly

                    # Manage Greeting Saving
                    if (getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False)) and self._save_as_greeting and greeting_buffer:
                        save_wav(bytes(greeting_buffer), greeting_path, OUT_RATE)
                        print(f"[BrowserWS] Greeting saved to {greeting_path}", flush=True)
                        self._save_as_greeting = False
                        greeting_buffer.clear()
                        try:
                            await self._send_browser_session_ready()
                        except Exception:
                            pass

                    # Handle Barge-in (Interrupted)
                    if getattr(sc, "interrupted", False):
                        print("[BrowserWS] Gemini interrupted — sending clear queue command", flush=True)
                        try:
                            await self.send(text_data=json.dumps({"event": "clear"}))
                        except Exception:
                            pass  # WS may be closing

                    # Save agent turn to history and detect goodbye
                    if getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False):
                        # Agent has finished speaking — clear the flag so the silence
                        # check can fire if the user doesn't respond.
                        if self._agent_speaking:
                            self._agent_speaking = False
                            # Signal frontend to unmute mic (with a short delay for echo decay)
                            try:
                                await self.send(text_data=json.dumps({"type": "agent_speaking", "value": False}))
                            except Exception:
                                pass
                        if self._current_agent_turn:
                            self._call_history.append({"role": "agent", "text": self._current_agent_turn.strip()})
                            idx = self._current_agent_turn.lower()

                            # Check if goodbye was said
                            goodbye_detected = any(phrase in idx for phrase in ["allah hafiz", "اللہ حافظ", "khuda hafiz", "goodbye", "bye"])

                            # Check if a terminal tool was ever successfully called
                            terminal_tool_called = any(
                                h.get("tool_name") in ["book_appointment", "place_order"]
                                and not (
                                    isinstance(h.get("tool_result"), dict)
                                    and h["tool_result"].get("error")
                                )
                                for h in self._call_history
                            ) or getattr(self, "_booking_state", None) == "booked"

                            # Detect if order details were being collected but tool
                            # was never called — covers "booking_requested" state (set
                            # when place_order is first attempted) AND broader heuristic
                            # for when the model spoke order-related filler but skipped
                            # the actual tool invocation.
                            order_in_progress = (
                                getattr(self, "_booking_state", None) in ("booking_requested", "confirmed")
                            )
                            # Also check if the agent spoke order-placement filler phrases
                            # (indicating intent to call place_order) in this turn or any
                            # prior agent turn — if so the tool MUST have been called.
                            _ORDER_FILLER_PHRASES = [
                                "order laga",
                                "placing your order",
                                "placing the order",
                                "order place",
                                "order enter kar",
                                "order system mein",
                                "main aap ka order",
                            ]
                            agent_spoke_order_filler = any(
                                any(phrase in h.get("text", "").lower() for phrase in _ORDER_FILLER_PHRASES)
                                for h in self._call_history
                                if h.get("role") == "agent"
                            )

                            if terminal_tool_called and self._pending_tool_calls == 0:
                                print("[BrowserWS] Terminal booking/order completed — scheduling disconnect after final response.", flush=True)
                                self._schedule_auto_close(6.0, "terminal_response_completed")
                            elif goodbye_detected:
                                if self._pending_tool_calls > 0:
                                    print(f"[BrowserWS] Model said goodbye but {self._pending_tool_calls} tool call(s) pending — NOT disconnecting.", flush=True)
                                elif (order_in_progress or agent_spoke_order_filler) and not terminal_tool_called:
                                    # CRITICAL SAFETY NET: The model said goodbye but the
                                    # order/booking was never actually placed via the API.
                                    # Force-nudge the model to call the tool NOW.
                                    print(
                                        f"[BrowserWS] CRITICAL: Order/booking in progress "
                                        f"(state={getattr(self, '_booking_state', '')}, "
                                        f"filler_spoken={agent_spoke_order_filler}) but "
                                        f"terminal tool was NEVER called! Blocking disconnect "
                                        f"and nudging model to call terminal tool.",
                                        flush=True,
                                    )
                                    self._should_end_call = False
                                    terminal_tool = "book_appointment" if self._get_agent_key() == "healthcare" else "place_order"
                                    # Nudge Gemini to actually invoke the tool
                                    try:
                                        await session.send_realtime_input(
                                            text=(
                                                "[System: CRITICAL — You said goodbye but you "
                                                f"did NOT call the {terminal_tool} tool. The booking/order has "
                                                f"NOT been saved. You MUST call the {terminal_tool} "
                                                "tool RIGHT NOW with all the order details before "
                                                "ending this call. Do NOT say goodbye again until "
                                                "the tool has been called and you received a result.]"
                                            )
                                        )
                                    except Exception as nudge_exc:
                                        print(f"[BrowserWS] Nudge to call tool failed: {nudge_exc}", flush=True)
                                else:
                                    print(f"[BrowserWS] Detected goodbye — scheduling disconnect.", flush=True)
                                    self._schedule_auto_close(6.0, "goodbye_detected")
                            else:
                                # Agent finished speaking — user's turn; start silence timeout
                                # so if they go quiet we nudge them gently.
                                self._schedule_silence_check()
                            self._current_agent_turn = ""

                        if self._should_end_call:
                            self._schedule_auto_close(6.0, "call_marked_complete")

        except ConnectionClosed as exc:
            print(f"[BrowserWS] Browser receive loop closed: {exc}", flush=True)
