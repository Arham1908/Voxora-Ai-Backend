import hashlib
import logging
import random
import socket
import string
import threading
import time
import uuid
from pyVoIP.VoIP import CallState

from ._logging import log_sip_tx, log_sip_rx
from ._constants import SIP_RATE, MIC_RATE, OUT_RATE, FRAME_DURATION

logger = logging.getLogger(__name__)


class MultinetRegistrar:
    AUTH_IP     = "ip"
    AUTH_DIGEST = "digest"
    AUTH_NONE   = "none"

    def __init__(self, server, port, username, password, local_ip, local_port, public_ip=None, on_registered=None, on_failed=None):
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
        logger.info("[Registrar] Init: server=%s:%d user=%s local=%s:%d public=%s", server, port, username, local_ip, local_port, self.public_ip)

    @staticmethod
    def _rand_str(n=8): return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

    def _gen_call_id(self): return f"{self._rand_str(16)}@{self.local_ip}"

    @staticmethod
    def _md5(s): return hashlib.md5(s.encode()).hexdigest()

    def _build_digest_auth(self, realm, nonce, opaque, qop):
        ha1    = self._md5(f"{self.username}:{realm}:{self.password}")
        uri    = f"sip:{self.server}"
        ha2    = self._md5(f"REGISTER:{uri}")
        cnonce = self._rand_str(16)
        nc     = "00000001"
        if qop:
            resp = self._md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            auth = f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", uri="{uri}", qop={qop}, nc={nc}, cnonce="{cnonce}", response="{resp}", algorithm=MD5'
        else:
            resp = self._md5(f"{ha1}:{nonce}:{ha2}")
            auth = f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}", algorithm=MD5'
        if opaque:
            auth += f', opaque="{opaque}"'
        return auth

    def _parse_header(self, msg, header):
        for line in msg.split("\r\n"):
            if line.lower().startswith(header.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    def _parse_www_auth(self, msg):
        raw = self._parse_header(msg, "WWW-Authenticate") or self._parse_header(msg, "Proxy-Authenticate")
        if not raw: return {}
        raw = raw[7:].strip() if raw.lower().startswith("digest ") else raw
        result = {}
        for part in raw.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = v.strip().strip('"')
        return result

    def _build_register(self, expires=3600, auth_header=None):
        self._cseq += 1
        branch = f"z9hG4bK{self._rand_str(16)}"
        lines = [
            f"REGISTER sip:{self.server} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.public_ip}:{self.local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.username}@{self.server}>;tag={self._tag}",
            f"To: <sip:{self.username}@{self.server}>",
            f"Call-ID: {self._call_id}",
            f"CSeq: {self._cseq} REGISTER",
            f"Contact: <sip:{self.username}@{self.public_ip}:{self.local_port}>",
            f"Expires: {expires}",
            "User-Agent: BlenSpark-VoiceAgent/1.0",
        ]
        if auth_header:
            lines.append(f"Authorization: {auth_header}")
        lines += ["Content-Length: 0", "", ""]
        return "\r\n".join(lines)

    def _send_recv(self, msg, timeout=5.0):
        dest = (self.server, self.port)
        log_sip_tx("REGISTER", msg, dest)
        self._sock.sendto(msg.encode(), dest)
        self._sock.settimeout(timeout)
        try:
            data, addr = self._sock.recvfrom(4096)
            resp = data.decode("utf-8", errors="replace")
            log_sip_rx(resp.split("\r\n")[0] if resp else "(empty)", resp, addr)
            return resp
        except socket.timeout:
            logger.warning("[Registrar] No response from %s:%d (timeout=%ss)", self.server, self.port, timeout)
            return ""

    @staticmethod
    def _status_code(resp):
        parts = resp.split(" ", 2)
        return parts[1] if len(parts) >= 2 else "?"

    def _do_register(self, expires=3600):
        logger.info("[Registrar] REGISTER attempt (mode=%s, expires=%d)", self._auth_mode, expires)
        resp1 = self._send_recv(self._build_register(expires=expires))
        if not resp1:
            logger.error("[Registrar] No response — server unreachable or IP blocked")
            return False
        status = self._status_code(resp1)
        if status == "200":
            if self._auth_mode != self.AUTH_IP:
                logger.info("[Registrar] IP-based auth confirmed")
                self._auth_mode = self.AUTH_IP
            return True
        if status in ("401", "407"):
            logger.info("[Registrar] Digest challenge (status=%s)", status)
            self._auth_mode = self.AUTH_DIGEST
            auth_params = self._parse_www_auth(resp1)
            nonce  = auth_params.get("nonce", "")
            realm  = auth_params.get("realm", self.server)
            opaque = auth_params.get("opaque", "")
            qop    = auth_params.get("qop", "")
            if not nonce:
                logger.error("[Registrar] No nonce in 401")
                return False
            auth_header = self._build_digest_auth(realm, nonce, opaque, qop)
            resp2  = self._send_recv(self._build_register(expires=expires, auth_header=auth_header))
            status2 = self._status_code(resp2) if resp2 else "?"
            if status2 == "200":
                logger.info("[Registrar] Digest auth OK — registered!")
                return True
            elif status2 == "403":
                logger.error("[Registrar] 403 Forbidden — wrong password or IP not whitelisted")
            elif status2 in ("401", "407"):
                logger.error("[Registrar] Still getting %s — check SIP_PASSWORD", status2)
            else:
                logger.error("[Registrar] Unexpected status2: %s", status2)
            return False
        if status == "403":
            logger.error("[Registrar] 403 on first REGISTER — IP %s blocked by Multinet", self.public_ip)
            return False
        logger.error("[Registrar] Unexpected status: %s", status)
        return False

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self.local_ip, self.local_port))
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="MultinetRegistrar")
        self._thread.start()
        logger.info("[Registrar] Started on %s:%d -> %s:%d", self.local_ip, self.local_port, self.server, self.port)

    def stop(self):
        logger.info("[Registrar] Stopping...")
        self._running = False
        try:
            self._do_register(expires=0)
        except Exception as e:
            logger.debug("[Registrar] De-register error: %s", e)
        if self._sock:
            self._sock.close()

    def get_socket(self):
        return self._sock

    def make_call(self, to_number, on_answered, on_failed=None, local_rtp_port=12000, agent_id="healthcare", voice="Aoede", language="ur-PK"):
        threading.Thread(target=self._do_invite, args=(to_number, on_answered, on_failed, local_rtp_port, agent_id, voice, language), daemon=True, name=f"OutboundCall-{to_number}").start()

    def _do_invite(self, to_number, on_answered, on_failed, local_rtp_port, agent_id, voice, language):
        from .server import RawSIPCall
        call_id  = self._gen_call_id()
        tag      = self._rand_str(8)
        branch   = f"z9hG4bK{self._rand_str(16)}"
        cseq_num = 1
        to_uri   = f"sip:{to_number}@{self.server}"
        from_uri = f"sip:{self.username}@{self.server}"
        sdp      = self._build_invite_sdp(local_rtp_port)
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
        out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            out_sock.bind((self.local_ip, 0))
            dest = (self.server, self.port)
            log_sip_tx("INVITE (outbound)", invite, dest)
            out_sock.sendto(invite.encode(), dest)
            out_sock.settimeout(30.0)
            while True:
                data, addr = out_sock.recvfrom(65535)
                resp = data.decode("utf-8", errors="replace")
                first_line = resp.split("\r\n")[0]
                log_sip_rx(first_line, resp, addr)
                status = self._status_code(resp)
                if status in ("100", "180", "183"):
                    continue
                if status in ("401", "407"):
                    auth_params  = self._parse_www_auth(resp)
                    auth_header  = self._build_digest_auth_invite(
                        auth_params.get("realm", self.server),
                        auth_params.get("nonce", ""),
                        auth_params.get("opaque", ""),
                        auth_params.get("qop", ""),
                        to_uri,
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
                    log_sip_tx("INVITE (auth retry)", invite2, dest)
                    out_sock.sendto(invite2.encode(), dest)
                    continue
                if status == "200":
                    remote_tag   = self._parse_header(resp, "To")
                    remote_rtp_ip, remote_rtp_port = self._parse_sdp_rtp_from_200(resp, addr[0])
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
                    log_sip_tx("ACK", ack, dest)
                    out_sock.sendto(ack.encode(), dest)
                    call = RawSIPCall(
                        sip_sock=out_sock, remote_addr=addr,
                        remote_rtp_ip=remote_rtp_ip, remote_rtp_port=remote_rtp_port,
                        local_rtp_port=local_rtp_port, caller=to_number, call_id=call_id,
                        via=f"SIP/2.0/UDP {self.public_ip}:{self.local_port};branch={branch}",
                        from_h=f"<{from_uri}>;tag={tag}", to_h=remote_tag, tag=tag,
                        cseq=f"{cseq_num} INVITE",
                    )
                    call.answer()
                    def bye_received():
                        call._state   = CallState.ENDED
                        call._running = False
                    threading.Thread(target=self._outbound_sip_listener, args=(out_sock, call, bye_received), daemon=True, name="OutboundBYEListener").start()
                    on_answered(call)
                    return
                if status in ("486", "603", "480", "404", "403", "408"):
                    if on_failed: on_failed(status)
                    return
            if on_failed: on_failed("timeout")
        except socket.timeout:
            if on_failed: on_failed("timeout")
        except Exception as e:
            logger.error("[Registrar] Outbound error: %s", e, exc_info=True)
            if on_failed: on_failed(str(e))
        finally:
            try: out_sock.close()
            except Exception: pass

    def _build_invite_sdp(self, rtp_port):
        pub = self.public_ip
        return f"v=0\r\no=blenspark 0 0 IN IP4 {pub}\r\ns=BlenSpark VoiceAgent\r\nc=IN IP4 {pub}\r\nt=0 0\r\nm=audio {rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\n"

    def _build_digest_auth_invite(self, realm, nonce, opaque, qop, to_uri):
        ha1    = self._md5(f"{self.username}:{realm}:{self.password}")
        ha2    = self._md5(f"INVITE:{to_uri}")
        cnonce = self._rand_str(16)
        nc     = "00000001"
        if qop:
            resp = self._md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            auth = f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", uri="{to_uri}", qop={qop}, nc={nc}, cnonce="{cnonce}", response="{resp}", algorithm=MD5'
        else:
            resp = self._md5(f"{ha1}:{nonce}:{ha2}")
            auth = f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", uri="{to_uri}", response="{resp}", algorithm=MD5'
        if opaque: auth += f', opaque="{opaque}"'
        return auth

    def _parse_sdp_rtp_from_200(self, msg, fallback_ip):
        rtp_ip, rtp_port = fallback_ip, 0
        in_sdp = False
        for line in msg.split("\r\n"):
            if line == "": in_sdp = True
            if in_sdp:
                if line.startswith("c=IN IP4 "):
                    rtp_ip = line.split("c=IN IP4 ")[1].strip()
                elif line.startswith("m=audio "):
                    parts = line.split()
                    if len(parts) >= 2:
                        try: rtp_port = int(parts[1])
                        except ValueError: pass
        return rtp_ip, rtp_port

    def _outbound_sip_listener(self, out_sock, call, on_bye):
        out_sock.settimeout(1.0)
        while call._running:
            try:
                data, addr = out_sock.recvfrom(4096)
                msg = data.decode("utf-8", errors="replace")
                first = msg.split("\r\n")[0] if "\r\n" in msg else msg.split("\n")[0]
                if first.startswith("BYE"):
                    h_call_id = self._parse_header(msg, "Call-ID")
                    h_cseq    = self._parse_header(msg, "CSeq")
                    h_via     = self._parse_header(msg, "Via")
                    h_from    = self._parse_header(msg, "From")
                    h_to      = self._parse_header(msg, "To")
                    ok = f"SIP/2.0 200 OK\r\nVia: {h_via}\r\nFrom: {h_from}\r\nTo: {h_to}\r\nCall-ID: {h_call_id}\r\nCSeq: {h_cseq}\r\nContent-Length: 0\r\n\r\n"
                    log_sip_tx("200 OK (BYE)", ok, addr)
                    out_sock.sendto(ok.encode(), addr)
                    on_bye()
                    return
            except socket.timeout: continue
            except Exception: break

    def _loop(self):
        retry_delay = 5
        while self._running:
            ok = self._do_register()
            self.registered = ok
            if ok:
                retry_delay = 5
                if self.on_registered:
                    try: self.on_registered()
                    except Exception: pass
                for _ in range(55):
                    if not self._running: break
                    time.sleep(1)
            else:
                if self.on_failed:
                    try: self.on_failed()
                    except Exception: pass
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
