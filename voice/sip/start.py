import time
import logging

from .server import SIPServer

logger = logging.getLogger(__name__)


def start_sip_server(agent_id="healthcare", voice="Aoede", language="ur-PK"):
    server = SIPServer(agent_id=agent_id, voice=voice, language=language)
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down SIP server...")
    finally:
        server.stop()
