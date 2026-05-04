"""
SIP Client — pyVoIP to Gemini Live bridge.

Flow:
  Phone call → RawSIPServer (SIP + RTP) → SIPCallBridge → Gemini Live API
                                         ← SIPCallBridge ←

Modes:
  multinet  — Python registers directly to Multinet (IP-auth + Digest fallback)
  asterisk  — Python registers to local Asterisk via pyVoIP; Asterisk handles Multinet
  local     — MicroSIP softphone registers to Python directly (dev/testing)

Auth modes (multinet):
  1. IP-based  — no password needed, Multinet whitelists your public IP
  2. Digest    — full RFC 3261 qop=auth MD5 (fallback if IP auth fails)
"""

import asyncio
import audioop
import hashlib
import json
import logging
import os
import random
import socket
import string
import struct
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# pyVoIP — CallState + InvalidStateError always needed;
# VoIPPhone only imported in asterisk mode (lazy import inside __init__)
from pyVoIP.VoIP import InvalidStateError, CallState

logger = logging.getLogger(__name__)

# ── Audio format constants ────────────────────────────────────────────
SIP_RATE       = 8000    # G.711 µ-law from SIP
MIC_RATE       = 16000   # Gemini input
OUT_RATE       = 24000   # Gemini output
FRAME_DURATION = 0.02    # 20 ms RTP frames

# ── Gemini ────────────────────────────────────────────────────────────
try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError("pip install google-genai")


# ─────────────────────────────────────────────────────────────────────
# Logging helpers
# ─────────────────────────────────────────────────────────────────────

def _log_sip_tx(label: str, msg: str, addr):
    lines = msg.strip().split("\r\n") if "\r\n" in msg else msg.strip().split("\n")
    logger.debug("[SIP TX ▶ %s:%d] ── %s", addr[0], addr[1], label)
    for line in lines:
        if line.strip():
            logger.debug("[SIP TX]  %s", line)
    logger.debug("[SIP TX] ──────────────────────────────────────────")


def _log_sip_rx(label: str, msg: str, addr):
    lines = msg.strip().split("\r\n") if "\r\n" in msg else msg.strip().split("\n")
    logger.debug("[SIP RX ◀ %s:%d] ── %s", addr[0], addr[1], label)
    for line in lines:
        if line.strip():
            logger.debug("[SIP RX]  %s", line)
    logger.debug("[SIP RX] ──────────────────────────────────────────")


def _get_local_ip() -> str:
    """Auto-detect LAN IP — works at flat, office, Multinet office, Docker."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return "10.99.39.11"
    except Exception:
        return "127.0.0.1"


# ─────────────────────────────────────────────────────────────────────
# MultinetRegistrar — outbound REGISTER with IP-auth + Digest fallback
# ─────────────────────────────────────────────────────────────────────

class MultinetRegistrar:
    """
    Registers to Multinet SIP trunk and keeps registration alive.

    Strategy (auto-detected on first attempt):
      1. IP-based auth   — REGISTER → 200 OK directly (no password needed)
      2. Digest auth     — REGISTER → 401 → compute RFC 3261 qop=auth MD5 → retry
                           403 means wrong password.

    Re-registers every 55 s (before 60 s expiry).
    """

    AUTH_IP     = "ip"
    AUTH_DIGEST = "digest"
    AUTH_NONE   = "none"

    def __init__(
        self,
        server: str,
        port: int,
        username: str,
        password: str,
        local_ip: str,
        local_port: int,
        public_ip: str = None,
        on_registered=None,
        on_failed=None,
    ):
        self.server     = server
        self.port       = port
        self.username   = username
        self.password   = password
        self.local_ip   = local_ip
        self.local_port = local_port
        self.public_ip  = public_ip or local_ip
        self.on_registered = on_registered
        self.on_failed     = on_failed

        self._running   = False
        self._sock      = None
        self._thread    = None
        self._cseq      = 0
        self._call_id   = self._gen_call_id()
        self._tag       = self._rand_str(8)
        self._auth_mode = self.AUTH_NONE
        self.registered = False

        logger.info(
            "[Registrar] Init: server=%s:%d user=%s local=%s:%d public=%s",
            server, port, username, local_ip, local_port, self.public_ip,
        )

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _rand_str(n=8) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

    def _gen_call_id(self) -> str:
        return f"{self._rand_str(16)}@{self.local_ip}"

    @staticmethod
    def _md5(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest()

    def _build_digest_auth(self, realm, nonce, opaque, qop) -> str:
        ha1    = self._md5(f"{self.username}:{realm}:{self.password}")
        uri    = f"sip:{self.server}"
        ha2    = self._md5(f"REGISTER:{uri}")
        cnonce = self._rand_str(16)
        nc     = "00000001"

        if qop:
            resp = self._md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            auth = (
                f'Digest username="{self.username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", '
                f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                f'response="{resp}", algorithm=MD5'
            )
        else:
            resp = self._md5(f"{ha1}:{nonce}:{ha2}")
            auth = (
                f'Digest username="{self.username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", '
                f'response="{resp}", algorithm=MD5'
            )

        if opaque:
            auth += f', opaque="{opaque}"'

        logger.debug(
            "[Registrar] Digest: realm=%s nonce=%s qop=%s nc=%s cnonce=%s",
            realm, nonce, qop, nc, cnonce,
        )
        return auth

    def _parse_header(self, msg: str, header: str) -> str:
        for line in msg.split("\r\n"):
            if line.lower().startswith(header.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    def _parse_www_auth(self, msg: str) -> dict:
        raw = self._parse_header(msg, "WWW-Authenticate")
        if not raw:
            raw = self._parse_header(msg, "Proxy-Authenticate")
        if not raw:
            return {}
        raw = raw[7:].strip() if raw.lower().startswith("digest ") else raw
        result = {}
        for part in raw.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = v.strip().strip('"')
        logger.debug("[Registrar] WWW-Auth parsed: %s", result)
        return result

    def _build_register(self, expires=3600, auth_header=None) -> str:
        self._cseq += 1
        branch = f"z9hG4bK{self._rand_str(16)}"
        lines = [
            f"REGISTER sip:{self.server} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.public_ip}:{self.local_port};branch={branch};rport",
            f"Max-Forwards: 70",
            f"From: <sip:{self.username}@{self.server}>;tag={self._tag}",
            f"To: <sip:{self.username}@{self.server}>",
            f"Call-ID: {self._call_id}",
            f"CSeq: {self._cseq} REGISTER",
            f"Contact: <sip:{self.username}@{self.public_ip}:{self.local_port}>",
            f"Expires: {expires}",
            f"User-Agent: BlenSpark-VoiceAgent/1.0",
        ]
        if auth_header:
            lines.append(f"Authorization: {auth_header}")
        lines += ["Content-Length: 0", "", ""]
        return "\r\n".join(lines)

    # ── network ───────────────────────────────────────────────────────

    def _send_recv(self, msg: str, timeout=5.0) -> str:
        dest = (self.server, self.port)
        _log_sip_tx("REGISTER", msg, dest)
        self._sock.sendto(msg.encode(), dest)
        self._sock.settimeout(timeout)
        try:
            data, addr = self._sock.recvfrom(4096)
            resp = data.decode("utf-8", errors="replace")
            _log_sip_rx(resp.split("\r\n")[0] if resp else "(empty)", resp, addr)
            return resp
        except socket.timeout:
            logger.warning("[Registrar] ⏱ No response from %s:%d (timeout=%ss)", self.server, self.port, timeout)
            return ""

    # ── core registration logic ───────────────────────────────────────

    def _do_register(self, expires=3600) -> bool:
        logger.info("[Registrar] ── REGISTER attempt (mode=%s, expires=%d) ──", self._auth_mode, expires)

        # Step 1 — plain REGISTER (no auth)
        resp1 = self._send_recv(self._build_register(expires=expires))
        if not resp1:
            logger.error("[Registrar] ✗ No response — server unreachable or IP blocked")
            return False

        status = self._status_code(resp1)
        logger.info("[Registrar] Step 1 → %s", status)

        if status == "200":
            if self._auth_mode != self.AUTH_IP:
                logger.info("[Registrar] ✅ IP-based auth confirmed")
                self._auth_mode = self.AUTH_IP
            return True

        if status in ("401", "407"):
            logger.info("[Registrar] 🔐 Digest challenge (status=%s)", status)
            self._auth_mode = self.AUTH_DIGEST
            auth_params = self._parse_www_auth(resp1)
            nonce  = auth_params.get("nonce", "")
            realm  = auth_params.get("realm", self.server)
            opaque = auth_params.get("opaque", "")
            qop    = auth_params.get("qop", "")

            if not nonce:
                logger.error("[Registrar] ✗ No nonce in 401")
                return False

            auth_header = self._build_digest_auth(realm, nonce, opaque, qop)
            resp2  = self._send_recv(self._build_register(expires=expires, auth_header=auth_header))
            status2 = self._status_code(resp2) if resp2 else "?"
            logger.info("[Registrar] Step 2 → %s", status2)

            if status2 == "200":
                logger.info("[Registrar] ✅ Digest auth OK — registered!")
                return True
            elif status2 == "403":
                logger.error("[Registrar] ✗ 403 Forbidden — wrong password or IP not whitelisted")
            elif status2 in ("401", "407"):
                logger.error("[Registrar] ✗ Still getting %s — check SIP_PASSWORD", status2)
            else:
                logger.error("[Registrar] ✗ Unexpected status2: %s", status2)
            return False

        if status == "403":
            logger.error("[Registrar] ✗ 403 on first REGISTER — IP %s blocked by Multinet", self.public_ip)
            return False

        logger.error("[Registrar] ✗ Unexpected status: %s", status)
        return False

    @staticmethod
    def _status_code(resp: str) -> str:
        parts = resp.split(" ", 2)
        return parts[1] if len(parts) >= 2 else "?"

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self.local_ip, self.local_port))
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="MultinetRegistrar")
        self._thread.start()
        logger.info("[Registrar] Started on %s:%d → %s:%d", self.local_ip, self.local_port, self.server, self.port)

    def stop(self):
        logger.info("[Registrar] Stopping...")
        self._running = False
        try:
            self._do_register(expires=0)
            logger.info("[Registrar] De-registered")
        except Exception as e:
            logger.debug("[Registrar] De-register error: %s", e)
        if self._sock:
            self._sock.close()

    def get_socket(self):
        """Return the bound socket so RawSIPServer can share it."""
        return self._sock

    def make_call(self, to_number: str, on_answered, on_failed=None,
                  local_rtp_port=12000, agent_id="healthcare", voice="Aoede", language="ur-PK"):
        """Trigger an outbound call to to_number (async)."""
        threading.Thread(
            target=self._do_invite,
            args=(to_number, on_answered, on_failed, local_rtp_port, agent_id, voice, language),
            daemon=True,
            name=f"OutboundCall-{to_number}",
        ).start()

    def _do_invite(self, to_number, on_answered, on_failed,
                   local_rtp_port, agent_id, voice, language):
        """Send INVITE to Multinet for outbound call."""
        call_id  = self._gen_call_id()
        tag      = self._rand_str(8)
        branch   = f"z9hG4bK{self._rand_str(16)}"
        cseq_num = 1

        to_uri   = f"sip:{to_number}@{self.server}"
        from_uri = f"sip:{self.username}@{self.server}"

        sdp = self._build_invite_sdp(local_rtp_port)

        invite = (
            f"INVITE {to_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.public_ip}:{self.local_port};branch={branch};rport\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <{from_uri}>;tag={tag}\r\n"
            f"To: <{to_uri}>\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq_num} INVITE\r\n"
            f"Contact: <sip:{self.username}@{self.public_ip}:{self.local_port}>\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
        )

        # Use separate socket for outbound to avoid conflicts with registration loop
        out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            out_sock.bind((self.local_ip, 0))
            dest = (self.server, self.port)
            _log_sip_tx("INVITE (outbound)", invite, dest)
            out_sock.sendto(invite.encode(), dest)

            # Wait for 200 OK (with retry on 407/401)
            out_sock.settimeout(30.0)
            remote_tag = ""
            try:
                while True:
                    data, addr = out_sock.recvfrom(65535)
                    resp = data.decode("utf-8", errors="replace")
                    first_line = resp.split("\r\n")[0]
                    _log_sip_rx(first_line, resp, addr)

                    status = self._status_code(resp)

                    if status in ("100", "180", "183"):
                        logger.info("[Registrar] Outbound: %s (%s)", status, to_number)
                        continue

                    if status in ("401", "407"):
                        logger.info("[Registrar] Outbound: auth challenge %s", status)
                        auth_params  = self._parse_www_auth(resp)
                        nonce  = auth_params.get("nonce", "")
                        realm  = auth_params.get("realm", self.server)
                        opaque = auth_params.get("opaque", "")
                        qop    = auth_params.get("qop", "")
                        auth_header = self._build_digest_auth_invite(
                            realm, nonce, opaque, qop, to_uri
                        )
                        cseq_num += 1
                        branch2 = f"z9hG4bK{self._rand_str(16)}"
                        invite2 = (
                            f"INVITE {to_uri} SIP/2.0\r\n"
                            f"Via: SIP/2.0/UDP {self.public_ip}:{self.local_port};branch={branch2};rport\r\n"
                            f"Max-Forwards: 70\r\n"
                            f"From: <{from_uri}>;tag={tag}\r\n"
                            f"To: <{to_uri}>\r\n"
                            f"Call-ID: {call_id}\r\n"
                            f"CSeq: {cseq_num} INVITE\r\n"
                            f"Contact: <sip:{self.username}@{self.public_ip}:{self.local_port}>\r\n"
                            f"Authorization: {auth_header}\r\n"
                            f"Content-Type: application/sdp\r\n"
                            f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
                        )
                        _log_sip_tx("INVITE (auth retry)", invite2, dest)
                        out_sock.sendto(invite2.encode(), dest)
                        continue

                    if status == "200":
                        logger.info("[Registrar] ✅ Outbound call answered: %s", to_number)
                        remote_tag = self._parse_header(resp, "To")
                        remote_rtp_ip, remote_rtp_port = self._parse_sdp_rtp_from_200(resp, addr[0])

                        # Send ACK
                        ack = (
                            f"ACK {to_uri} SIP/2.0\r\n"
                            f"Via: SIP/2.0/UDP {self.public_ip}:{self.local_port};branch={self._rand_str(16)};rport\r\n"
                            f"Max-Forwards: 70\r\n"
                            f"From: <{from_uri}>;tag={tag}\r\n"
                            f"To: {remote_tag}\r\n"
                            f"Call-ID: {call_id}\r\n"
                            f"CSeq: {cseq_num} ACK\r\n"
                            f"Content-Length: 0\r\n\r\n"
                        )
                        _log_sip_tx("ACK", ack, dest)
                        out_sock.sendto(ack.encode(), dest)

                        # Build call shim
                        call = RawSIPCall(
                            sip_sock=out_sock,
                            remote_addr=addr,
                            remote_rtp_ip=remote_rtp_ip,
                            remote_rtp_port=remote_rtp_port,
                            local_rtp_port=local_rtp_port,
                            caller=to_number,
                            call_id=call_id,
                            via=f"SIP/2.0/UDP {self.public_ip}:{self.local_port};branch={branch}",
                            from_h=f"<{from_uri}>;tag={tag}",
                            to_h=remote_tag,
                            tag=tag,
                            cseq=f"{cseq_num} INVITE",
                        )
                        call.answer()

                        def bye_received():
                            logger.info("[Registrar] Remote hung up — stopping call")
                            call._state   = CallState.ENDED
                            call._running = False

                        threading.Thread(
                            target=self._outbound_sip_listener,
                            args=(out_sock, call, bye_received),
                            daemon=True,
                            name="OutboundBYEListener",
                        ).start()

                        on_answered(call)
                        return

                    if status in ("486", "603", "480", "404", "403", "408"):
                        logger.error("[Registrar] Outbound call failed: %s %s", status, to_number)
                        if on_failed:
                            on_failed(status)
                        return

            except socket.timeout:
                logger.error("[Registrar] Outbound INVITE timeout: %s", to_number)
                if on_failed:
                    on_failed("timeout")

        except Exception as e:
            logger.error("[Registrar] Outbound socket error: %s", e, exc_info=True)
            if on_failed:
                on_failed(str(e))
        finally:
            try:
                out_sock.close()
            except Exception:
                pass

    def _build_invite_sdp(self, rtp_port: int) -> str:
        return (
            f"v=0\r\n"
            f"o=blenspark 0 0 IN IP4 {self.public_ip}\r\n"
            f"s=BlenSpark VoiceAgent\r\n"
            f"c=IN IP4 {self.public_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=ptime:20\r\n"
        )

    def _build_digest_auth_invite(self, realm, nonce, opaque, qop, to_uri) -> str:
        """Build digest auth for INVITE (URI = INVITE target, not REGISTER)."""
        ha1    = self._md5(f"{self.username}:{realm}:{self.password}")
        uri    = to_uri
        ha2    = self._md5(f"INVITE:{uri}")
        cnonce = self._rand_str(16)
        nc     = "00000001"
        if qop:
            resp = self._md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            auth = (
                f'Digest username="{self.username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", '
                f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                f'response="{resp}", algorithm=MD5'
            )
        else:
            resp = self._md5(f"{ha1}:{nonce}:{ha2}")
            auth = (
                f'Digest username="{self.username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", '
                f'response="{resp}", algorithm=MD5'
            )
        if opaque:
            auth += f', opaque="{opaque}"'
        return auth

    def _parse_sdp_rtp_from_200(self, msg: str, fallback_ip: str):
        """Extract remote RTP IP:port from 200 OK SDP."""
        rtp_ip, rtp_port = fallback_ip, 0
        in_sdp = False
        for line in msg.split("\r\n"):
            if line == "":
                in_sdp = True
            if in_sdp:
                if line.startswith("c=IN IP4 "):
                    rtp_ip = line.split("c=IN IP4 ")[1].strip()
                elif line.startswith("m=audio "):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            rtp_port = int(parts[1])
                        except ValueError:
                            pass
        return rtp_ip, rtp_port

    def _outbound_sip_listener(self, out_sock, call, on_bye):
        """Listen for BYE on outbound socket after call established."""
        out_sock.settimeout(1.0)
        while call._running:
            try:
                data, addr = out_sock.recvfrom(4096)
                msg = data.decode("utf-8", errors="replace")
                first = msg.split("\r\n")[0] if "\r\n" in msg else msg.split("\n")[0]

                if first.startswith("BYE"):
                    logger.info("[Registrar] 🔔 Remote BYE received — ending outbound call")
                    h_call_id = self._parse_header(msg, "Call-ID")
                    h_cseq    = self._parse_header(msg, "CSeq")
                    h_via     = self._parse_header(msg, "Via")
                    h_from    = self._parse_header(msg, "From")
                    h_to      = self._parse_header(msg, "To")
                    ok = (
                        f"SIP/2.0 200 OK\r\nVia: {h_via}\r\nFrom: {h_from}\r\n"
                        f"To: {h_to}\r\nCall-ID: {h_call_id}\r\nCSeq: {h_cseq}\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    _log_sip_tx("200 OK (BYE)", ok, addr)
                    out_sock.sendto(ok.encode(), addr)
                    on_bye()
                    return
            except socket.timeout:
                continue
            except Exception:
                break

    def _loop(self):
        """Keep-alive loop — re-registers every 55 s with exponential backoff on failure."""
        retry_delay = 5
        while self._running:
            ok = self._do_register()
            self.registered = ok

            if ok:
                retry_delay = 5
                if self.on_registered:
                    try:
                        self.on_registered()
                    except Exception:
                        pass
                logger.info("[Registrar] 💤 Next re-register in 55s")
                for _ in range(55):
                    if not self._running:
                        break
                    time.sleep(1)
            else:
                if self.on_failed:
                    try:
                        self.on_failed()
                    except Exception:
                        pass
                logger.warning("[Registrar] ⚠ Failed, retry in %ds", retry_delay)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)


# ─────────────────────────────────────────────────────────────────────
# SIPCallBridge — one per active call
# ─────────────────────────────────────────────────────────────────────

class SIPCallBridge:
    """
    Bridges a single SIP call to Gemini Live.
    Works with ANY call object that exposes:
      .state, .answer(), .read_audio(), .write_audio(), .hangup()
    This means it works identically for:
      - pyVoIP VoIPCall  (asterisk mode)
      - RawSIPCall shim  (multinet / local mode)
    """

    def __init__(self, call, agent_id="healthcare", voice="Aoede", language="ur-PK"):
        self.call     = call
        self.agent_id = agent_id
        self.voice    = voice
        self.language = language

        self._session_uuid   = str(uuid.uuid4())
        self._running        = False
        self._gemini_session = None
        self._loop           = None

        self._upsample_state   = None   # 8kHz → 16kHz
        self._downsample_state = None   # 24kHz → 8kHz

        self._start_time    = time.time()
        self._usage_metrics = {
            "prompt": 0, "response": 0, "total": 0,
            "input_text": 0, "input_audio": 0,
            "output_text": 0, "output_audio": 0,
        }
        self._call_history       = []
        self._current_agent_turn = ""

        logger.info(
            "[Bridge %s] Created: agent=%s voice=%s lang=%s",
            self._session_uuid[:8], agent_id, voice, language,
        )

    def start(self):
        self._running = True
        t = threading.Thread(
            target=self._run_async_loop, daemon=True,
            name=f"Bridge-{self._session_uuid[:8]}",
        )
        t.start()
        logger.info("[Bridge %s] Thread started", self._session_uuid[:8])

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
            logger.info("[Bridge %s] Thread ended", self._session_uuid[:8])

    async def _run_gemini_session(self):
        from .agents.registry import get_agent

        agent_cfg = get_agent(self.agent_id)
        if not agent_cfg:
            logger.error("[Bridge %s] Unknown agent: %s", self._session_uuid[:8], self.agent_id)
            return

        schedule_data = await self._fetch_schedule_data()

        greeting_path_fn = agent_cfg.get("greeting_path_fn")
        greeting_path    = (
            greeting_path_fn(self.language, self.voice)
            if greeting_path_fn
            else agent_cfg["greeting_path"]
        )
        has_cached_greeting = greeting_path.exists()

        system_prompt = agent_cfg["build_system_prompt"](
            language=self.language,
            voice=self.voice,
            has_cached_greeting=has_cached_greeting,
            schedule_data=schedule_data,
        )
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
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                ),
                language_code=self.language,
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
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
            async with client.aio.live.connect(
                model="gemini-3.1-flash-live-preview", config=live_config
            ) as session:
                logger.info(
                    "[Bridge %s] Gemini Live connected in %.2fs",
                    self._session_uuid[:8], time.time() - t0,
                )
                self._gemini_session = session

                await self._handle_greeting(session, agent_cfg, greeting_path, has_cached_greeting)

                tasks = [
                    asyncio.create_task(self._sip_to_gemini(session)),
                    asyncio.create_task(self._gemini_to_sip(session, agent_cfg)),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()

        except Exception as e:
            logger.error("[Bridge %s] Gemini session error: %s", self._session_uuid[:8], e, exc_info=True)
        finally:
            self._gemini_session = None
            await self._save_session_cost()
            self._cleanup_call()

    async def _fetch_schedule_data(self) -> list:
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
            logger.info("[Bridge %s] Playing cached greeting: %s", self._session_uuid[:8], greeting_path)
            pcm = self._load_wav_pcm(greeting_path)
            self._write_pcm24k_to_sip(pcm)
        else:
            gen_fn = agent_cfg.get("generate_greeting_prompt_fn")
            prompt = gen_fn(self.language, self.voice) if gen_fn else agent_cfg.get("greeting_prompt", "Greet warmly.")
            logger.info("[Bridge %s] Generating greeting via Gemini", self._session_uuid[:8])
            self._save_as_greeting   = True
            self._greeting_buffer    = bytearray()
            self._greeting_save_path = greeting_path
            await session.send_realtime_input(text=prompt)

    def _load_wav_pcm(self, path: Path) -> bytes:
        with wave.open(str(path), "rb") as wf:
            return wf.readframes(wf.getnframes())

    def _write_pcm24k_to_sip(self, pcm_24k: bytes):
        try:
            pcm_8k, self._downsample_state = audioop.ratecv(
                pcm_24k, 2, 1, OUT_RATE, SIP_RATE, self._downsample_state
            )
            ulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
            for i in range(0, len(ulaw_8k), 160):
                if not self._running:
                    break
                try:
                    self.call.write_audio(ulaw_8k[i:i + 160])
                except (InvalidStateError, OSError):
                    logger.info("[Bridge %s] Call ended during audio write", self._session_uuid[:8])
                    self._running = False
                    break
                time.sleep(FRAME_DURATION)
        except Exception as e:
            logger.error("[Bridge %s] Audio write error: %s", self._session_uuid[:8], e)

    async def _sip_to_gemini(self, session):
        logger.info("[Bridge %s] ▶ SIP→Gemini loop started", self._session_uuid[:8])
        frames = 0
        try:
            while self._running:
                try:
                    if self.call.state != CallState.ANSWERED:
                        logger.info("[Bridge %s] Call state: %s — stopping", self._session_uuid[:8], self.call.state)
                        break
                except Exception:
                    break

                try:
                    ulaw = await asyncio.get_event_loop().run_in_executor(None, self._read_sip_audio)
                except Exception:
                    break

                if not ulaw:
                    await asyncio.sleep(0.01)
                    continue

                pcm_8k  = audioop.ulaw2lin(ulaw, 2)
                pcm_16k, self._upsample_state = audioop.ratecv(
                    pcm_8k, 2, 1, SIP_RATE, MIC_RATE, self._upsample_state
                )

                try:
                    await session.send_realtime_input(
                        audio=types.Blob(data=pcm_16k, mime_type=f"audio/pcm;rate={MIC_RATE}")
                    )
                    frames += 1
                    if frames == 1:
                        logger.info("[Bridge %s] ▶ First audio frame sent to Gemini", self._session_uuid[:8])
                    elif frames % 500 == 0:
                        logger.debug("[Bridge %s] ▶ %d frames sent to Gemini", self._session_uuid[:8], frames)
                except Exception as e:
                    logger.error("[Bridge %s] Gemini send error: %s", self._session_uuid[:8], e)
                    break

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("[Bridge %s] ▶ SIP→Gemini loop ended (%d frames)", self._session_uuid[:8], frames)

    def _read_sip_audio(self) -> bytes:
        try:
            data = self.call.read_audio(length=160, blocking=True)
            return data if data else b""
        except InvalidStateError:
            self._running = False
            return b""
        except Exception:
            return b""

    async def _gemini_to_sip(self, session, agent_cfg):
        logger.info("[Bridge %s] ◀ Gemini→SIP loop started", self._session_uuid[:8])
        greeting_buffer  = bytearray()
        save_as_greeting = getattr(self, "_save_as_greeting", False)

        try:
            while self._running:
                async for response in session.receive():
                    if not self._running:
                        break

                    # usage metrics
                    usage = getattr(response, "usage_metadata", None)
                    if usage:
                        for attr, key in [
                            ("prompt_token_count",   "prompt"),
                            ("response_token_count", "response"),
                            ("total_token_count",    "total"),
                        ]:
                            val = getattr(usage, attr, 0) or 0
                            self._usage_metrics[key] = max(self._usage_metrics[key], val)

                    # tool calls
                    tool_call = getattr(response, "tool_call", None)
                    if tool_call:
                        fn_responses = []
                        for fc in tool_call.function_calls:
                            args   = dict(fc.args) if fc.args else {}
                            logger.info("[Bridge %s] 🔧 Tool: %s(%s)", self._session_uuid[:8], fc.name, args)
                            result = await self._execute_tool_fn(fc.name, args)
                            logger.info("[Bridge %s] 🔧 Result: %s → %s", self._session_uuid[:8], fc.name, result)
                            self._call_history.append({
                                "role": "tool", "tool_name": fc.name,
                                "tool_args": args, "tool_result": result,
                            })
                            fn_responses.append(
                                types.FunctionResponse(name=fc.name, id=fc.id, response={"result": result})
                            )
                        try:
                            await session.send_tool_response(function_responses=fn_responses)
                        except Exception as e:
                            logger.error("[Bridge %s] Tool response error: %s", self._session_uuid[:8], e)
                        continue

                    sc = getattr(response, "server_content", None)
                    if sc is None:
                        continue

                    if getattr(sc, "input_transcription", None):
                        t = sc.input_transcription
                        if hasattr(t, "text") and t.text:
                            logger.info("[Bridge %s] 👤 User: %s", self._session_uuid[:8], t.text)
                            self._call_history.append({"role": "user", "text": t.text})

                    if getattr(sc, "output_transcription", None):
                        t = sc.output_transcription
                        if hasattr(t, "text") and t.text:
                            logger.info("[Bridge %s] 🤖 Agent: %s", self._session_uuid[:8], t.text)
                            if not self._current_agent_turn.endswith(t.text):
                                self._current_agent_turn += t.text

                    if getattr(sc, "model_turn", None):
                        for part in sc.model_turn.parts:
                            if getattr(part, "text", None):
                                if not self._current_agent_turn.endswith(part.text):
                                    self._current_agent_turn += part.text
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                if save_as_greeting:
                                    greeting_buffer.extend(inline.data)
                                await asyncio.get_event_loop().run_in_executor(
                                    None, self._write_pcm24k_to_sip, inline.data
                                )

                    if getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False):
                        if save_as_greeting and greeting_buffer:
                            self._save_wav(bytes(greeting_buffer), getattr(self, "_greeting_save_path", None))
                            save_as_greeting = self._save_as_greeting = False
                            greeting_buffer.clear()

                        if self._current_agent_turn:
                            self._call_history.append({
                                "role": "agent",
                                "text": self._current_agent_turn.strip(),
                            })
                            idx = self._current_agent_turn.lower()
                            if any(p in idx for p in ["allah hafiz", "اللہ حافظ", "khuda hafiz", "goodbye", "bye"]):
                                logger.info("[Bridge %s] 👋 Goodbye — ending in 5s", self._session_uuid[:8])
                                await asyncio.sleep(5)
                                self._running = False
                                break
                            self._current_agent_turn = ""

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[Bridge %s] Receive error: %s", self._session_uuid[:8], e, exc_info=True)
        finally:
            self._running = False
            logger.info("[Bridge %s] ◀ Gemini→SIP loop ended", self._session_uuid[:8])

    def _save_wav(self, pcm: bytes, path):
        if not path:
            return
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(OUT_RATE)
                wf.writeframes(pcm)
            logger.info("[Bridge %s] Greeting saved: %s", self._session_uuid[:8], path)
        except Exception as e:
            logger.error("[Bridge %s] WAV save error: %s", self._session_uuid[:8], e)

    def _cleanup_call(self):
        try:
            if self.call.state == CallState.ANSWERED:
                self.call.hangup()
                logger.info("[Bridge %s] Call hung up", self._session_uuid[:8])
        except Exception as e:
            logger.debug("[Bridge %s] Hangup error: %s", self._session_uuid[:8], e)

    async def _save_session_cost(self):
        duration = int(time.time() - self._start_time)
        m = self._usage_metrics
        if m["total"] > 0 or duration > 0:
            try:
                from asgiref.sync import sync_to_async
                from Analytics.models import GeminiSessionCost
                total_cost = (
                    m["input_text"]   * 0.00000075 +
                    m["input_audio"]  * 0.000003   +
                    m["output_text"]  * 0.0000045  +
                    m["output_audio"] * 0.000012
                )
                await sync_to_async(GeminiSessionCost.objects.create)(
                    session_id=self._session_uuid,
                    agent_type=self.agent_id,
                    prompt_tokens=m["prompt"], response_tokens=m["response"],
                    total_tokens=m["total"],
                    input_text_tokens=m["input_text"], input_audio_tokens=m["input_audio"],
                    output_text_tokens=m["output_text"], output_audio_tokens=m["output_audio"],
                    call_duration_seconds=duration,
                    estimated_cost_usd=total_cost,
                )
                logger.info("[Bridge %s] Cost saved: $%.6f %ds", self._session_uuid[:8], total_cost, duration)
            except Exception as e:
                logger.error("[Bridge %s] Cost save failed: %s", self._session_uuid[:8], e)

        if self._call_history:
            try:
                from asgiref.sync import sync_to_async
                from Analytics.models import CallHistory
                await sync_to_async(CallHistory.objects.create)(
                    session_id=self._session_uuid,
                    agent_type=self.agent_id,
                    duration_seconds=duration,
                    transcript=self._call_history,
                )
                logger.info("[Bridge %s] History saved: %d turns", self._session_uuid[:8], len(self._call_history))
            except Exception as e:
                logger.error("[Bridge %s] History save failed: %s", self._session_uuid[:8], e)


# ─────────────────────────────────────────────────────────────────────
# RawSIPServer — UDP SIP listener
# Handles REGISTER + INVITE from Multinet or MicroSIP
# ─────────────────────────────────────────────────────────────────────

class RawSIPServer:
    """
    Minimal UDP SIP server.
    - local mode    : accepts REGISTER from MicroSIP → auto 200 OK
    - multinet mode : just listens for INVITE (registration done by MultinetRegistrar)
    """

    def __init__(self, bind_ip, bind_port, username, password,
                 on_call, agent_id, voice, language,
                 rtp_port_low=10000, rtp_port_high=20000,
                 shared_sock=None):
        self.bind_ip   = bind_ip
        self.bind_port = bind_port
        self.username  = username
        self.password  = password
        self.on_call   = on_call
        self.agent_id  = agent_id
        self.voice     = voice
        self.language  = language
        self.rtp_port_low  = rtp_port_low
        self.rtp_port_high = rtp_port_high
        self._shared_sock  = shared_sock
        self._running  = False
        self._sock     = None
        self._thread   = None
        self._rtp_port_counter = rtp_port_low

    def _next_rtp_port(self):
        port = self._rtp_port_counter
        self._rtp_port_counter += 2
        if self._rtp_port_counter > self.rtp_port_high:
            self._rtp_port_counter = self.rtp_port_low
        return port

    def start(self):
        self._running = True
        if self._shared_sock:
            self._sock = self._shared_sock
            logger.info("[RawSIP] Reusing shared socket on %s:%d", self.bind_ip, self.bind_port)
        else:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.bind_ip, self.bind_port))
            self._sock.settimeout(1.0)
            logger.info("[RawSIP] Listening on %s:%d", self.bind_ip, self.bind_port)
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="RawSIPServer")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._sock and not self._shared_sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _listen_loop(self):
        logger.info("[RawSIP] UDP listener running")
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error("[RawSIP] Socket error: %s", e)
                break
            try:
                msg   = data.decode("utf-8", errors="replace")
                first = msg.split("\r\n")[0] if "\r\n" in msg else msg.split("\n")[0]
                _log_sip_rx(first, msg, addr)
                self._handle_message(msg, addr)
            except Exception as e:
                logger.error("[RawSIP] Message handling error: %s", e, exc_info=True)
        logger.info("[RawSIP] UDP listener stopped")

    def _handle_message(self, msg: str, addr):
        first  = msg.split("\r\n")[0] if "\r\n" in msg else msg.split("\n")[0]
        method = first.split()[0] if first else ""
        dispatch = {
            "REGISTER": self._handle_register,
            "INVITE":   self._handle_invite,
            "BYE":      self._handle_bye,
            "CANCEL":   self._handle_cancel,
            "OPTIONS":  self._handle_options,
            "ACK":      lambda m, a: logger.debug("[RawSIP] ACK from %s:%d", a[0], a[1]),
        }
        handler = dispatch.get(method)
        if handler:
            handler(msg, addr)
        else:
            logger.debug("[RawSIP] Unhandled method: %s from %s:%d", method, addr[0], addr[1])

    def _parse_header(self, msg: str, header: str) -> str:
        for line in msg.split("\r\n"):
            if line.lower().startswith(header.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    def _common_headers(self, msg: str) -> dict:
        return {
            "call_id": self._parse_header(msg, "Call-ID"),
            "cseq":    self._parse_header(msg, "CSeq"),
            "from_h":  self._parse_header(msg, "From"),
            "to_h":    self._parse_header(msg, "To"),
            "via":     self._parse_header(msg, "Via"),
        }

    def _send(self, response: str, addr, label="response"):
        _log_sip_tx(label, response, addr)
        try:
            self._sock.sendto(response.encode("utf-8"), addr)
        except Exception as e:
            logger.error("[RawSIP] Send error: %s", e)

    def _handle_register(self, msg: str, addr):
        h = self._common_headers(msg)
        self._send(
            f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']};tag=blenspark{int(time.time())}\r\n"
            f"Call-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Contact: <sip:{self.username}@{self.bind_ip}:{self.bind_port}>\r\n"
            f"Expires: 3600\r\nContent-Length: 0\r\n\r\n",
            addr, "200 OK (REGISTER)",
        )
        logger.info("[RawSIP] REGISTER 200 OK → %s:%d", addr[0], addr[1])

    def _handle_options(self, msg: str, addr):
        h = self._common_headers(msg)
        self._send(
            f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER\r\n"
            f"Content-Length: 0\r\n\r\n",
            addr, "200 OK (OPTIONS)",
        )

    def _handle_bye(self, msg: str, addr):
        h = self._common_headers(msg)
        self._send(
            f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Content-Length: 0\r\n\r\n",
            addr, "200 OK (BYE)",
        )
        logger.info("[RawSIP] BYE 200 OK → %s:%d", addr[0], addr[1])

    def _handle_cancel(self, msg: str, addr):
        h = self._common_headers(msg)
        self._send(
            f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Content-Length: 0\r\n\r\n",
            addr, "200 OK (CANCEL)",
        )

    def _handle_invite(self, msg: str, addr):
        h = self._common_headers(msg)
        logger.info("[RawSIP] 📞 INVITE from %s:%d (Call-ID=%s)", addr[0], addr[1], h["call_id"])

        remote_rtp_ip, remote_rtp_port = self._parse_sdp_rtp(msg, addr[0])
        local_rtp_port = self._next_rtp_port()
        tag = f"blenspark{int(time.time())}"

        logger.info(
            "[RawSIP] SDP: remote RTP=%s:%d  local RTP port=%d",
            remote_rtp_ip, remote_rtp_port, local_rtp_port,
        )

        self._send(
            f"SIP/2.0 100 Trying\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Content-Length: 0\r\n\r\n",
            addr, "100 Trying",
        )
        self._send(
            f"SIP/2.0 180 Ringing\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']};tag={tag}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Content-Length: 0\r\n\r\n",
            addr, "180 Ringing",
        )

        sdp = self._build_sdp_answer(local_rtp_port)
        self._send(
            f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\n"
            f"To: {h['to_h']};tag={tag}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\n"
            f"Contact: <sip:{self.username}@{self.bind_ip}:{self.bind_port}>\r\n"
            f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}",
            addr, "200 OK (INVITE)",
        )

        call = RawSIPCall(
            sip_sock=self._sock, remote_addr=addr,
            remote_rtp_ip=remote_rtp_ip, remote_rtp_port=remote_rtp_port,
            local_rtp_port=local_rtp_port, caller=h["from_h"],
            call_id=h["call_id"], via=h["via"], from_h=h["from_h"],
            to_h=h["to_h"], tag=tag, cseq=h["cseq"],
        )
        threading.Thread(target=self.on_call, args=(call,), daemon=True).start()

    def _parse_sdp_rtp(self, msg: str, fallback_ip: str):
        rtp_ip, rtp_port = fallback_ip, 0
        in_sdp = False
        for line in msg.split("\r\n"):
            if line == "":
                in_sdp = True
            if in_sdp:
                if line.startswith("c=IN IP4 "):
                    rtp_ip = line.split("c=IN IP4 ")[1].strip()
                elif line.startswith("m=audio "):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            rtp_port = int(parts[1])
                        except ValueError:
                            pass
        return rtp_ip, rtp_port

    def _build_sdp_answer(self, local_rtp_port: int) -> str:
        return (
            f"v=0\r\n"
            f"o=blenspark 0 0 IN IP4 {self.bind_ip}\r\n"
            f"s=BlenSpark Voice Agent\r\n"
            f"c=IN IP4 {self.bind_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {local_rtp_port} RTP/AVP 0\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=ptime:20\r\n"
        )


# ─────────────────────────────────────────────────────────────────────
# RawSIPCall — RTP shim (mimics pyVoIP call interface)
# Used in multinet + local modes
# ─────────────────────────────────────────────────────────────────────

class RawSIPCall:

    def __init__(self, sip_sock, remote_addr, remote_rtp_ip, remote_rtp_port,
                 local_rtp_port, caller, call_id, via, from_h, to_h, tag, cseq):
        self.caller   = caller
        self.call_id  = call_id
        self._sip_sock        = sip_sock
        self._remote_addr     = remote_addr
        self._remote_rtp_ip   = remote_rtp_ip
        self._remote_rtp_port = remote_rtp_port
        self._local_rtp_port  = local_rtp_port
        self._via    = via
        self._from_h = from_h
        self._to_h   = to_h
        self._tag    = tag
        self._cseq   = cseq
        self._state  = CallState.ANSWERED
        self._rtp_sock     = None
        self._audio_buffer = bytearray()
        self._buffer_lock  = threading.Lock()
        self._running      = False
        self._seq          = 0
        self._ts           = 0
        self._ssrc         = int(uuid.uuid4()) & 0xFFFFFFFF
        self._rtp_rx_count = 0
        self._rtp_tx_count = 0

    @property
    def state(self):
        return self._state

    def answer(self):
        self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp_sock.bind(("0.0.0.0", self._local_rtp_port))
        self._rtp_sock.settimeout(0.1)
        self._running = True
        threading.Thread(target=self._rtp_recv_loop, daemon=True, name="RTPRecv").start()
        logger.info(
            "[RawSIPCall] RTP active: local_port=%d  remote=%s:%d",
            self._local_rtp_port, self._remote_rtp_ip, self._remote_rtp_port,
        )

    def _rtp_recv_loop(self):
        logger.info("[RawSIPCall] RTP recv loop started")
        last_packet_time = time.time()
        SILENCE_TIMEOUT  = 30.0

        while self._running and self._state == CallState.ANSWERED:
            try:
                data, _ = self._rtp_sock.recvfrom(4096)
                if len(data) > 12:
                    last_packet_time = time.time()
                    payload = data[12:]
                    with self._buffer_lock:
                        self._audio_buffer.extend(payload)
                    self._rtp_rx_count += 1
                    if self._rtp_rx_count == 1:
                        logger.info("[RawSIPCall] 🎤 First RTP packet received")
                    elif self._rtp_rx_count % 500 == 0:
                        logger.debug("[RawSIPCall] 🎤 RTP RX: %d packets", self._rtp_rx_count)
            except socket.timeout:
                if time.time() - last_packet_time > SILENCE_TIMEOUT:
                    logger.warning("[RawSIPCall] ⏱ RTP silence timeout (>%ds) — ending call", int(SILENCE_TIMEOUT))
                    self._state   = CallState.ENDED
                    self._running = False
                    break
                continue
            except Exception:
                break
        logger.info("[RawSIPCall] RTP recv loop ended (rx=%d tx=%d)", self._rtp_rx_count, self._rtp_tx_count)

    def read_audio(self, length=160, blocking=True) -> bytes:
        waited = 0
        while blocking and self._running:
            with self._buffer_lock:
                if len(self._audio_buffer) >= length:
                    chunk = bytes(self._audio_buffer[:length])
                    del self._audio_buffer[:length]
                    return chunk
            time.sleep(0.005)
            waited += 5
            if waited > 1000:
                return b""
        with self._buffer_lock:
            if len(self._audio_buffer) >= length:
                chunk = bytes(self._audio_buffer[:length])
                del self._audio_buffer[:length]
                return chunk
        return b""

    def write_audio(self, data: bytes):
        if not self._running or not self._rtp_sock:
            raise InvalidStateError("Call not active")
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts  = (self._ts  + len(data)) & 0xFFFFFFFF
        header = struct.pack("!BBHII", 0x80, 0x00, self._seq, self._ts, self._ssrc)
        try:
            self._rtp_sock.sendto(header + data, (self._remote_rtp_ip, self._remote_rtp_port))
            self._rtp_tx_count += 1
            if self._rtp_tx_count == 1:
                logger.info("[RawSIPCall] 🔊 First RTP packet sent")
            elif self._rtp_tx_count % 500 == 0:
                logger.debug("[RawSIPCall] 🔊 RTP TX: %d packets", self._rtp_tx_count)
        except Exception as e:
            raise OSError(f"RTP send failed: {e}")

    def hangup(self):
        self._state   = CallState.ENDED
        self._running = False
        if self._rtp_sock:
            try:
                self._rtp_sock.close()
            except Exception:
                pass
        try:
            cseq_num = int(self._cseq.split()[0]) + 1
            bye = (
                f"BYE sip:{self._remote_addr[0]} SIP/2.0\r\n"
                f"Via: {self._via}\r\nFrom: {self._from_h}\r\n"
                f"To: {self._to_h};tag={self._tag}\r\n"
                f"Call-ID: {self.call_id}\r\nCSeq: {cseq_num} BYE\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            _log_sip_tx("BYE", bye, self._remote_addr)
            self._sip_sock.sendto(bye.encode(), self._remote_addr)
            logger.info("[RawSIPCall] BYE sent")
        except Exception as e:
            logger.debug("[RawSIPCall] BYE send error: %s", e)


# ─────────────────────────────────────────────────────────────────────
# SIPServer — top-level orchestrator
#
# MODE: multinet
#   MultinetRegistrar → sends REGISTER to Multinet (IP-auth + Digest fallback)
#   RawSIPServer      → listens for INVITEs from Multinet on same socket
#   SIPCallBridge     → bridges RTP ↔ Gemini Live
#
# MODE: asterisk                                  ← NEW
#   pyVoIP VoIPPhone → registers to local Asterisk (Asterisk handles Multinet)
#   Asterisk sends INVITE to Python via pyVoIP
#   SIPCallBridge     → bridges RTP ↔ Gemini Live
#   Python IP is auto-detected — works at flat / office / Docker
#
# MODE: local
#   RawSIPServer      → MicroSIP registers here
#   SIPCallBridge     → bridges RTP ↔ Gemini Live
# ─────────────────────────────────────────────────────────────────────

class SIPServer:

    def __init__(self, agent_id="healthcare", voice="Aoede", language="ur-PK"):
        from .sip_config import (
            SIP_MODE,
            SIP_BIND_IP, SIP_BIND_PORT,
            SIP_SERVER, SIP_SERVER_PORT,
            SIP_USERNAME, SIP_PASSWORD,
            SIP_PUBLIC_IP,
            SIP_TEST_USERNAME, SIP_TEST_PASSWORD,
            SIP_RTP_PORT_LOW, SIP_RTP_PORT_HIGH,
            # asterisk-mode settings
            ASTERISK_HOST, ASTERISK_PORT,
            ASTERISK_USERNAME, ASTERISK_PASSWORD,
        )

        self.agent_id = agent_id
        self.voice    = voice
        self.language = language
        self.mode     = SIP_MODE

        # Auto-detect local LAN IP (works at any location / Docker)
        local_ip  = SIP_BIND_IP if SIP_BIND_IP and SIP_BIND_IP != "0.0.0.0" else _get_local_ip()
        public_ip = "10.99.39.11"
        # public_ip = SIP_PUBLIC_IP if SIP_PUBLIC_IP else local_ip

        logger.info(
            "[SIPServer] Init: mode=%s  local_ip=%s  public_ip=%s",
            SIP_MODE, local_ip, public_ip,
        )

        # ── MULTINET MODE ──────────────────────────────────────────────
        # Python registers directly to Multinet.
        # MultinetRegistrar owns the socket; RawSIPServer shares it.
        # Call path: Multinet → RawSIPServer → SIPCallBridge → Gemini
        # ──────────────────────────────────────────────────────────────
        if SIP_MODE == "multinet":
            if not SIP_SERVER or not SIP_USERNAME:
                raise ValueError("SIP_SERVER and SIP_USERNAME required for multinet mode.")

            self._registrar = MultinetRegistrar(
                server=SIP_SERVER,
                port=SIP_SERVER_PORT,
                username=SIP_USERNAME,
                password=SIP_PASSWORD,
                local_ip=local_ip,
                local_port=SIP_BIND_PORT,
                public_ip=public_ip,
                on_registered=self._on_registered,
                on_failed=self._on_registration_failed,
            )
            self._sip_server = RawSIPServer(
                bind_ip=local_ip,
                bind_port=SIP_BIND_PORT,
                username=SIP_USERNAME,
                password=SIP_PASSWORD,
                on_call=self._on_incoming_call,
                agent_id=agent_id, voice=voice, language=language,
                rtp_port_low=SIP_RTP_PORT_LOW,
                rtp_port_high=SIP_RTP_PORT_HIGH,
            )
            self._phone          = None
            self._local_ip       = local_ip
            self._public_ip      = public_ip
            self._bind_port      = SIP_BIND_PORT
            self._multinet_addr  = SIP_SERVER
            self._multinet_port  = SIP_SERVER_PORT
            self._username       = SIP_USERNAME

        # ── ASTERISK MODE ──────────────────────────────────────────────
        # pyVoIP registers to local Asterisk.
        # Asterisk handles Multinet registration separately (pjsip.conf).
        # Call path: Multinet → Asterisk → pyVoIP → SIPCallBridge → Gemini
        #
        # Python's own IP is auto-detected each startup, so the same .env
        # works at flat (192.168.x.x), office (10.x.x.x), and Docker
        # (container IP). Only ASTERISK_HOST changes per environment.
        # ──────────────────────────────────────────────────────────────
        elif SIP_MODE == "asterisk":
            if not ASTERISK_HOST:
                raise ValueError(
                    "ASTERISK_HOST must be set for asterisk mode.\n"
                    "  Docker:  ASTERISK_HOST=asterisk  (service name)\n"
                    "  Flat:    ASTERISK_HOST=192.168.x.x\n"
                    "  Office:  ASTERISK_HOST=10.x.x.x"
                )

            # Lazy import — VoIPPhone only needed in this mode
            from pyVoIP.VoIP import VoIPPhone

            logger.info(
                "[SIPServer] Asterisk mode: local_ip=%s → asterisk=%s:%d  ext=%s  sip_port=%d",
                local_ip, ASTERISK_HOST, ASTERISK_PORT, ASTERISK_USERNAME, SIP_BIND_PORT,
            )

            # myIP tells pyVoIP what to put in REGISTER Contact header
            # → Asterisk uses this to know where to send INVITEs
            # → auto-detected so it's always correct regardless of location
            self._phone = VoIPPhone(
                server=ASTERISK_HOST,
                port=ASTERISK_PORT,
                username=ASTERISK_USERNAME,
                password=ASTERISK_PASSWORD,
                callCallback=self._on_incoming_call,
                myIP=local_ip,           # ← auto LAN IP, never hardcoded
                sipPort=SIP_BIND_PORT,   # Python listens here for Asterisk's INVITE
                rtpPortLow=SIP_RTP_PORT_LOW,
                rtpPortHigh=SIP_RTP_PORT_HIGH,
            )
            self._registrar      = None
            self._sip_server     = None
            self._local_ip       = local_ip
            self._bind_port      = SIP_BIND_PORT
            self._asterisk_host  = ASTERISK_HOST
            self._asterisk_port  = ASTERISK_PORT
            self._asterisk_user  = ASTERISK_USERNAME

        # ── LOCAL MODE ─────────────────────────────────────────────────
        # MicroSIP softphone registers directly to Python.
        # No Multinet, no Asterisk — pure local dev/testing.
        # Call path: MicroSIP → RawSIPServer → SIPCallBridge → Gemini
        # ──────────────────────────────────────────────────────────────
        else:
            self._registrar = None
            self._phone     = None
            self._sip_server = RawSIPServer(
                bind_ip=local_ip,
                bind_port=SIP_BIND_PORT,
                username=SIP_TEST_USERNAME,
                password=SIP_TEST_PASSWORD,
                on_call=self._on_incoming_call,
                agent_id=agent_id, voice=voice, language=language,
                rtp_port_low=SIP_RTP_PORT_LOW,
                rtp_port_high=SIP_RTP_PORT_HIGH,
            )
            self._local_ip      = local_ip
            self._bind_port     = SIP_BIND_PORT
            self._test_username = SIP_TEST_USERNAME
            self._test_password = SIP_TEST_PASSWORD

        self._outbound_rtp_counter = SIP_RTP_PORT_HIGH - 100

    # ── callbacks ─────────────────────────────────────────────────────

    def _on_registered(self):
        logger.info("[SIPServer] ✅ Registered to Multinet — ready for calls")

    def _on_registration_failed(self):
        logger.error("[SIPServer] ❌ Registration failed — incoming calls will NOT arrive")

    def _next_outbound_rtp_port(self) -> int:
        """Auto-assign next RTP port for outbound calls."""
        port = self._outbound_rtp_counter
        self._outbound_rtp_counter += 2
        if self._outbound_rtp_counter > 20000:
            self._outbound_rtp_counter = 19900
        return port

    def make_outbound_call(self, to_number: str, local_rtp_port=None):
        """Trigger an outbound call to to_number (only in multinet mode)."""
        if self.mode != "multinet":
            logger.error("[SIPServer] Outbound only supported in multinet mode currently")
            return

        if local_rtp_port is None:
            local_rtp_port = self._next_outbound_rtp_port()

        logger.info("[SIPServer] 📲 Outbound call → %s (RTP port %d)", to_number, local_rtp_port)
        print(f"\n📲 Calling {to_number}...", flush=True)

        def on_answered(call):
            logger.info("[SIPServer] ✅ Outbound answered by %s", to_number)
            print(f"✅ Connected! Bridging to {self.agent_id}...", flush=True)
            bridge = SIPCallBridge(
                call=call,
                agent_id=self.agent_id,
                voice=self.voice,
                language=self.language,
            )
            bridge.start()
            while bridge._running:
                try:
                    if call.state != CallState.ANSWERED:
                        bridge._running = False
                        break
                except Exception:
                    break
                time.sleep(0.5)
            logger.info("[SIPServer] 📴 Outbound call ended")
            print("📴 Call ended\n", flush=True)

        def on_failed(reason):
            logger.error("[SIPServer] ❌ Outbound call failed: %s → %s", to_number, reason)
            print(f"❌ Call failed: {reason}\n", flush=True)

        self._registrar.make_call(
            to_number=to_number,
            on_answered=on_answered,
            on_failed=on_failed,
            local_rtp_port=local_rtp_port,
            agent_id=self.agent_id,
            voice=self.voice,
            language=self.language,
        )

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self):
        print("\n" + "=" * 60)
        print("  BlenSpark SIP Server")
        print("=" * 60)

        # ── multinet ──────────────────────────────────────────────────
        if self.mode == "multinet":
            self._registrar.start()

            logger.info("[SIPServer] Waiting up to 15s for Multinet registration...")
            for i in range(15):
                if self._registrar.registered:
                    break
                time.sleep(1)
                if i % 5 == 4:
                    logger.info("[SIPServer] Still waiting... (%ds)", i + 1)

            if not self._registrar.registered:
                logger.warning(
                    "[SIPServer] ⚠ Not registered yet — starting listener anyway. "
                    "Registrar will keep retrying in background."
                )

            # Share the socket so RawSIPServer and registrar use the same port
            self._sip_server._shared_sock = self._registrar.get_socket()
            self._sip_server.start()

            print(f"\n  Mode:       MULTINET (direct)")
            print(f"  Server:     {self._multinet_addr}:{self._multinet_port}")
            print(f"  Username:   {self._username}")
            print(f"  Local IP:   {self._local_ip}")
            print(f"  Public IP:  {self._public_ip}")
            print(f"  Auth mode:  {self._registrar._auth_mode.upper()}")
            print(f"  Registered: {'✅ YES' if self._registrar.registered else '⚠ PENDING'}")

        # ── asterisk ──────────────────────────────────────────────────
        elif self.mode == "asterisk":
            logger.info(
                "[SIPServer] Starting pyVoIP → Asterisk (%s:%d) as ext %s",
                self._asterisk_host, self._asterisk_port, self._asterisk_user,
            )
            self._phone.start()

            print(f"\n  Mode:       ASTERISK BRIDGE")
            print(f"  Asterisk:   {self._asterisk_host}:{self._asterisk_port}")
            print(f"  Extension:  {self._asterisk_user}")
            print(f"  Python IP:  {self._local_ip}:{self._bind_port}")
            print(f"")
            print(f"  Call path:")
            print(f"    PSTN → Multinet → Asterisk:{self._asterisk_port}")
            print(f"         → Python:{self._bind_port} → Gemini Live")
            print(f"")
            print(f"  Location cheatsheet:")
            print(f"    Flat/Office  →  ASTERISK_HOST=<LAN IP of Asterisk machine>")
            print(f"    Docker       →  ASTERISK_HOST=asterisk  (service name)")
            print(f"    Multinet HQ  →  Switch SIP_MODE=multinet in .env")

        # ── local ─────────────────────────────────────────────────────
        else:
            self._sip_server.start()
            print(f"\n  Mode:     LOCAL (MicroSIP testing)")
            print(f"  SIP Host: {self._local_ip}:{self._bind_port}")
            print(f"  Username: {self._test_username}")
            print(f"  Password: {self._test_password}")
            print(f"\n  MicroSIP setup:")
            print(f"    SIP Server: {self._local_ip}")
            print(f"    Username:   {self._test_username}")
            print(f"    Password:   {self._test_password}")
            print(f"    Domain:     {self._local_ip}")

        print(f"\n  Agent:    {self.agent_id}")
        print(f"  Voice:    {self.voice}")
        print(f"  Language: {self.language}")
        print(f"\n  Waiting for incoming calls...")
        print("=" * 60 + "\n")

    def stop(self):
        logger.info("[SIPServer] Stopping...")
        if self.mode == "asterisk" and self._phone:
            logger.info("[SIPServer] Stopping pyVoIP phone...")
            self._phone.stop()
        else:
            if self._sip_server:
                self._sip_server.stop()
            if self._registrar:
                self._registrar.stop()

    # ── call handler — same for ALL modes ────────────────────────────
    # In asterisk mode:  call = pyVoIP VoIPCall object
    # In multinet/local: call = RawSIPCall shim
    # SIPCallBridge works with both — same .state/.answer()/.read_audio()/.write_audio()/.hangup() interface

    def _on_incoming_call(self, call):
        caller = getattr(call, "caller", "unknown")
        logger.info("[SIPServer] 📞 Incoming call from: %s", caller)
        print(f"\n📞 Incoming call from: {caller}", flush=True)

        try:
            call.answer()
            logger.info("[SIPServer] ✅ Call answered")
            print(f"✅ Bridging to {self.agent_id} agent...", flush=True)

            bridge = SIPCallBridge(
                call=call,
                agent_id=self.agent_id,
                voice=self.voice,
                language=self.language,
            )
            bridge.start()

            # Poll until call ends
            while bridge._running:
                try:
                    if call.state != CallState.ANSWERED:
                        logger.info("[SIPServer] Call state changed → %s, stopping bridge", call.state)
                        bridge._running = False
                        break
                except Exception:
                    break
                time.sleep(0.5)

            logger.info("[SIPServer] 📴 Call ended")
            print("📴 Call ended\n", flush=True)

        except InvalidStateError:
            logger.info("[SIPServer] Call already disconnected before answer")
        except Exception as e:
            logger.error("[SIPServer] Call handling error: %s", e, exc_info=True)
            try:
                call.hangup()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────
# Entry point — called from Django management command
# ─────────────────────────────────────────────────────────────────────

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