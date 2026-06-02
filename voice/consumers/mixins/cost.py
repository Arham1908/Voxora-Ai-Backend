import logging
import time

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


class CostTrackingMixin:
    _usage_metrics: dict
    _session_cost_saved: bool
    _start_time: float | None
    _session_uuid: str
    _call_history: list

    async def _save_session_cost(self):
        if getattr(self, "_session_cost_saved", False):
            return
        self._session_cost_saved = True

        duration = 0
        if self._start_time:
            duration = int(time.time() - self._start_time)

        agent_type = "healthcare"
        agent_cfg = getattr(self, "_agent_cfg", None)
        if agent_cfg:
            agent_type = agent_cfg.get("id", "healthcare")

        m = self._usage_metrics
        if m["total"] > 0 or duration > 0:
            try:
                from Analytics.models import GeminiSessionCost
                thinking_tokens = getattr(self, "_thinking_tokens_from_usage", 0) or 0
                output_text_with_thinking = m["output_text"] + thinking_tokens
                has_modality = (m["input_text"] + m["input_audio"] + m["output_text"] + m["output_audio"]) > 0

                if has_modality:
                    total_cost = (
                        float(m["input_text"]) * 0.00000075
                        + float(m["input_audio"]) * 0.000003
                        + float(output_text_with_thinking) * 0.0000045
                        + float(m["output_audio"]) * 0.000012
                    )
                    method = "modality"
                else:
                    total_cost = float(m["prompt"]) * 0.0000028875 + float(m["response"]) * 0.0000114
                    method = "blended_fallback"

                await sync_to_async(GeminiSessionCost.objects.create)(
                    session_id=self._session_uuid, agent_type=agent_type,
                    prompt_tokens=m["prompt"], response_tokens=m["response"], total_tokens=m["total"],
                    input_text_tokens=m["input_text"], input_audio_tokens=m["input_audio"],
                    output_text_tokens=output_text_with_thinking, output_audio_tokens=m["output_audio"],
                    call_duration_seconds=duration, estimated_cost_usd=total_cost, cost_calculation_method=method,
                )
            except Exception as e:
                logger.error("Failed to save session cost: %s", e)

        if self._call_history:
            try:
                from Analytics.models import CallHistory
                await sync_to_async(CallHistory.objects.create)(
                    session_id=self._session_uuid, agent_type=agent_type,
                    duration_seconds=duration, transcript=self._call_history,
                )
            except Exception as e:
                logger.error("Failed to save call history: %s", e)
