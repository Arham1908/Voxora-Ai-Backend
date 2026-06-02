import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)

_NUDGE_TIMEOUTS = [3.0, 5.0, 8.0]
_MAX_CONSECUTIVE_NUDGES = 3


class SilenceHandlerMixin:
    _disconnecting: bool
    gemini_session: object | None
    _last_user_input_time: float | None
    _consecutive_nudges: int
    _pending_tool_calls: int
    _agent_speaking: bool
    _silence_check_task: asyncio.Task | None
    _auto_close_task: asyncio.Task | None
    _auto_close_reason: str
    _should_end_call: bool
    _booking_state: str
    _tasks: list
    _session_uuid: str

    def _schedule_silence_check(self):
        if self._silence_check_task and not self._silence_check_task.done():
            return
        self._silence_check_task = asyncio.create_task(self._silence_timeout_handler())
        self._tasks.append(self._silence_check_task)

    def _cancel_silence_check(self):
        if self._silence_check_task and not self._silence_check_task.done():
            self._silence_check_task.cancel()
            self._silence_check_task = None

    def _mark_user_input(self):
        self._last_user_input_time = time.time()
        self._consecutive_nudges = 0
        self._cancel_auto_close_if_silence()

    def _cancel_auto_close_if_silence(self):
        if (
            getattr(self, "_auto_close_reason", "") == "silence_timeout_max_nudges"
            and self._auto_close_task
            and not self._auto_close_task.done()
        ):
            self._auto_close_task.cancel()
            self._auto_close_task = None
            self._auto_close_reason = ""
            self._should_end_call = False

    async def _silence_timeout_handler(self):
        if self._consecutive_nudges >= _MAX_CONSECUTIVE_NUDGES:
            self._schedule_auto_close(4.0, "silence_timeout_max_nudges")
            return
        idx = min(self._consecutive_nudges, len(_NUDGE_TIMEOUTS) - 1)
        await asyncio.sleep(_NUDGE_TIMEOUTS[idx])
        if self._disconnecting or self.gemini_session is None:
            return
        if time.time() - (self._last_user_input_time or 0) < 2.0:
            return
        if self._pending_tool_calls > 0 or self._agent_speaking:
            return
        if getattr(self, "_booking_state", None) == "booked":
            return
        self._consecutive_nudges += 1
        try:
            await self.gemini_session.send_realtime_input(text="[System: The user has been silent. Gently prompt them to continue.]")
        except Exception as e:
            logger.error("Nudge failed: %s", e)

    def _schedule_auto_close(self, delay: float = 6.0, reason: str = "auto_end"):
        if self._disconnecting or (self._auto_close_task and not self._auto_close_task.done()):
            return
        self._should_end_call = True
        self._auto_close_reason = reason
        self._auto_close_task = asyncio.create_task(self._delayed_close_with_reason(delay, reason))
        self._tasks.append(self._auto_close_task)

    async def _delayed_close_with_reason(self, delay: float, reason: str):
        await asyncio.sleep(delay)
        if not self._disconnecting:
            try:
                await self.send(text_data=json.dumps({"event": "call_end", "reason": reason}))
            except Exception:
                pass
            await self.close(code=1000)
