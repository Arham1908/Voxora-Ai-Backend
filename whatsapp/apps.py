import asyncio
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class WhatsappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "whatsapp"
    verbose_name = "WhatsApp Bot"

    def ready(self):
        """Capture main event loop when Django starts (for Daphne/ASGI)."""
        try:
            loop = asyncio.get_running_loop()
            from whatsapp.meta_views import _set_main_event_loop
            _set_main_event_loop(loop)
            logger.info("✅ WhatsApp app registered main event loop for async operations")
        except RuntimeError:
            # No running loop yet — will be set up by Daphne
            logger.debug("No running loop in app.ready() — will be available via Daphne ASGI")

