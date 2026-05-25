import logging
import socket

logger = logging.getLogger(__name__)


def log_sip_tx(label: str, msg: str, addr):
    lines = msg.strip().split("\r\n") if "\r\n" in msg else msg.strip().split("\n")
    logger.debug("[SIP TX ▶ %s:%d] ── %s", addr[0], addr[1], label)
    for line in lines:
        if line.strip():
            logger.debug("[SIP TX]  %s", line)
    logger.debug("[SIP TX] ──────────────────────────────────────────")


def log_sip_rx(label: str, msg: str, addr):
    lines = msg.strip().split("\r\n") if "\r\n" in msg else msg.strip().split("\n")
    logger.debug("[SIP RX ◀ %s:%d] ── %s", addr[0], addr[1], label)
    for line in lines:
        if line.strip():
            logger.debug("[SIP RX]  %s", line)
    logger.debug("[SIP RX] ──────────────────────────────────────────")


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
