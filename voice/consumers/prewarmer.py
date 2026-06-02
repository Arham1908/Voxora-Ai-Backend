import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class WarmSession:
    """Holds a live Gemini session open until the consumer releases it."""

    def __init__(self, session, task: asyncio.Task, release_event: asyncio.Event):
        self.session = session
        self._task = task
        self._release = release_event
        self.created_at = time.time()

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    def is_stale(self, max_age: float = 120.0) -> bool:
        return self.age > max_age

    async def release(self):
        self._release.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass


class AgentPrewarmer:
    """One pre-warmed session slot per agent type."""

    def __init__(self, agent_key: str):
        self.agent_key = agent_key
        self._ready: Optional[WarmSession] = None
        self._lock = asyncio.Lock()
        self._warming = False

    async def acquire(self) -> Optional[WarmSession]:
        async with self._lock:
            s = self._ready
            if s and not s.is_stale() and s.session is not None:
                self._ready = None
                logger.info(
                    "[Prewarmer:%s] Pre-warmed session handed off (age=%.1fs)",
                    self.agent_key, s.age,
                )
                return s
            if s and s.is_stale():
                logger.info("[Prewarmer:%s] Discarding stale session", self.agent_key)
                asyncio.create_task(s.release())
                self._ready = None
            return None

    async def warm(self, client, model: str, live_config):
        async with self._lock:
            if self._warming:
                return
            if self._ready and not self._ready.is_stale():
                return
            self._warming = True

        asyncio.create_task(self._do_warm(client, model, live_config))

    async def _do_warm(self, client, model: str, live_config, retries: int = 1):
        for attempt in range(retries + 1):
            release_event = asyncio.Event()
            ready_event = asyncio.Event()
            holder: list = []

            async def _hold_open():
                try:
                    async with client.aio.live.connect(model=model, config=live_config) as sess:
                        holder.append(sess)
                        ready_event.set()
                        await release_event.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("[Prewarmer:%s] Warm attempt %d failed: %s", self.agent_key, attempt + 1, exc)
                    ready_event.set()

            task = asyncio.create_task(_hold_open())

            try:
                await asyncio.wait_for(ready_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("[Prewarmer:%s] Warm attempt %d timed out", self.agent_key, attempt + 1)
                release_event.set()
                if attempt < retries:
                    continue
                async with self._lock:
                    self._warming = False
                return

            session = holder[0] if holder else None
            if session is None:
                if attempt < retries:
                    release_event.set()
                    continue
                async with self._lock:
                    self._warming = False
                return

            warm = WarmSession(session=session, task=task, release_event=release_event)
            async with self._lock:
                if self._ready:
                    asyncio.create_task(self._ready.release())
                self._ready = warm
                self._warming = False

            logger.info("[Prewarmer:%s] Session pre-warmed and ready", self.agent_key)
            return

        async with self._lock:
            self._warming = False


_warmers: dict[str, AgentPrewarmer] = {}

def get_prewarmer(agent_key: str) -> AgentPrewarmer:
    if agent_key not in _warmers:
        _warmers[agent_key] = AgentPrewarmer(agent_key)
    return _warmers[agent_key]
