import logging
import socket
import struct
import threading
import time
import uuid
from pyVoIP.VoIP import InvalidStateError, CallState

from ._logging import log_sip_tx, log_sip_rx
from ._constants import SIP_RATE, MIC_RATE, OUT_RATE, FRAME_DURATION

logger = logging.getLogger(__name__)


class RawSIPServer:
    def __init__(self, bind_ip, bind_port, username, password, on_call, agent_id, voice, language, rtp_port_low=10000, rtp_port_high=20000, shared_sock=None):
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
        else:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.bind_ip, self.bind_port))
            self._sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="RawSIPServer")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._sock and not self._shared_sock:
            try: self._sock.close()
            except Exception: pass

    def _listen_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout: continue
            except Exception as e:
                if self._running: logger.error("[RawSIP] Socket error: %s", e)
                break
            try:
                msg   = data.decode("utf-8", errors="replace")
                first = msg.split("\r\n")[0] if "\r\n" in msg else msg.split("\n")[0]
                log_sip_rx(first, msg, addr)
                self._handle_message(msg, addr)
            except Exception as e:
                logger.error("[RawSIP] Message error: %s", e, exc_info=True)

    def _handle_message(self, msg, addr):
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
        if handler: handler(msg, addr)

    def _parse_header(self, msg, header):
        for line in msg.split("\r\n"):
            if line.lower().startswith(header.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    def _common_headers(self, msg):
        return {"call_id": self._parse_header(msg, "Call-ID"), "cseq": self._parse_header(msg, "CSeq"), "from_h": self._parse_header(msg, "From"), "to_h": self._parse_header(msg, "To"), "via": self._parse_header(msg, "Via")}

    def _send(self, response, addr, label="response"):
        log_sip_tx(label, response, addr)
        try: self._sock.sendto(response.encode("utf-8"), addr)
        except Exception as e: logger.error("[RawSIP] Send error: %s", e)

    def _handle_register(self, msg, addr):
        h = self._common_headers(msg)
        self._send(f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\nTo: {h['to_h']};tag=blenspark{int(time.time())}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\nContact: <sip:{self.username}@{self.bind_ip}:{self.bind_port}>\r\nExpires: 3600\r\nContent-Length: 0\r\n\r\n", addr, "200 OK (REGISTER)")

    def _handle_options(self, msg, addr):
        h = self._common_headers(msg)
        self._send(f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\nTo: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\nAllow: INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER\r\nContent-Length: 0\r\n\r\n", addr, "200 OK (OPTIONS)")

    def _handle_bye(self, msg, addr):
        h = self._common_headers(msg)
        self._send(f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\nTo: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\nContent-Length: 0\r\n\r\n", addr, "200 OK (BYE)")

    def _handle_cancel(self, msg, addr):
        h = self._common_headers(msg)
        self._send(f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\nTo: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\nContent-Length: 0\r\n\r\n", addr, "200 OK (CANCEL)")

    def _handle_invite(self, msg, addr):
        h = self._common_headers(msg)
        remote_rtp_ip, remote_rtp_port = self._parse_sdp_rtp(msg, addr[0])
        local_rtp_port = self._next_rtp_port()
        tag = f"blenspark{int(time.time())}"
        self._send(f"SIP/2.0 100 Trying\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\nTo: {h['to_h']}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\nContent-Length: 0\r\n\r\n", addr, "100 Trying")
        self._send(f"SIP/2.0 180 Ringing\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\nTo: {h['to_h']};tag={tag}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\nContent-Length: 0\r\n\r\n", addr, "180 Ringing")
        sdp = self._build_sdp_answer(local_rtp_port)
        self._send(f"SIP/2.0 200 OK\r\nVia: {h['via']}\r\nFrom: {h['from_h']}\r\nTo: {h['to_h']};tag={tag}\r\nCall-ID: {h['call_id']}\r\nCSeq: {h['cseq']}\r\nContact: <sip:{self.username}@{self.bind_ip}:{self.bind_port}>\r\nContent-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}", addr, "200 OK (INVITE)")
        call = RawSIPCall(sip_sock=self._sock, remote_addr=addr, remote_rtp_ip=remote_rtp_ip, remote_rtp_port=remote_rtp_port, local_rtp_port=local_rtp_port, caller=h["from_h"], call_id=h["call_id"], via=h["via"], from_h=h["from_h"], to_h=h["to_h"], tag=tag, cseq=h["cseq"])
        threading.Thread(target=self.on_call, args=(call,), daemon=True).start()

    def _parse_sdp_rtp(self, msg, fallback_ip):
        rtp_ip, rtp_port = fallback_ip, 0
        in_sdp = False
        for line in msg.split("\r\n"):
            if line == "": in_sdp = True
            if in_sdp:
                if line.startswith("c=IN IP4 "): rtp_ip = line.split("c=IN IP4 ")[1].strip()
                elif line.startswith("m=audio "):
                    parts = line.split()
                    if len(parts) >= 2:
                        try: rtp_port = int(parts[1])
                        except ValueError: pass
        return rtp_ip, rtp_port

    def _build_sdp_answer(self, local_rtp_port):
        return f"v=0\r\no=blenspark 0 0 IN IP4 {self.bind_ip}\r\ns=BlenSpark Voice Agent\r\nc=IN IP4 {self.bind_ip}\r\nt=0 0\r\nm=audio {local_rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\n"


class RawSIPCall:
    def __init__(self, sip_sock, remote_addr, remote_rtp_ip, remote_rtp_port, local_rtp_port, caller, call_id, via, from_h, to_h, tag, cseq):
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
    def state(self): return self._state

    def answer(self):
        self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp_sock.bind(("0.0.0.0", self._local_rtp_port))
        self._rtp_sock.settimeout(0.1)
        self._running = True
        threading.Thread(target=self._rtp_recv_loop, daemon=True, name="RTPRecv").start()

    def _rtp_recv_loop(self):
        last_packet_time = time.time()
        SILENCE_TIMEOUT  = 30.0
        while self._running and self._state == CallState.ANSWERED:
            try:
                data, _ = self._rtp_sock.recvfrom(4096)
                if len(data) > 12:
                    last_packet_time = time.time()
                    with self._buffer_lock: self._audio_buffer.extend(data[12:])
                    self._rtp_rx_count += 1
            except socket.timeout:
                if time.time() - last_packet_time > SILENCE_TIMEOUT:
                    self._state = CallState.ENDED
                    self._running = False
                    break
                continue
            except Exception: break

    def read_audio(self, length=160, blocking=True):
        waited = 0
        while blocking and self._running:
            with self._buffer_lock:
                if len(self._audio_buffer) >= length:
                    chunk = bytes(self._audio_buffer[:length])
                    del self._audio_buffer[:length]
                    return chunk
            time.sleep(0.005)
            waited += 5
            if waited > 1000: return b""
        with self._buffer_lock:
            if len(self._audio_buffer) >= length:
                chunk = bytes(self._audio_buffer[:length])
                del self._audio_buffer[:length]
                return chunk
        return b""

    def write_audio(self, data):
        if not self._running or not self._rtp_sock:
            raise InvalidStateError("Call not active")
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts  = (self._ts + len(data)) & 0xFFFFFFFF
        header = struct.pack("!BBHII", 0x80, 0x00, self._seq, self._ts, self._ssrc)
        try:
            self._rtp_sock.sendto(header + data, (self._remote_rtp_ip, self._remote_rtp_port))
            self._rtp_tx_count += 1
        except Exception as e:
            raise OSError(f"RTP send failed: {e}")

    def hangup(self):
        self._state   = CallState.ENDED
        self._running = False
        if self._rtp_sock:
            try: self._rtp_sock.close()
            except Exception: pass
        try:
            cseq_num = int(self._cseq.split()[0]) + 1
            bye = f"BYE sip:{self._remote_addr[0]} SIP/2.0\r\nVia: {self._via}\r\nFrom: {self._from_h}\r\nTo: {self._to_h};tag={self._tag}\r\nCall-ID: {self.call_id}\r\nCSeq: {cseq_num} BYE\r\nContent-Length: 0\r\n\r\n"
            log_sip_tx("BYE", bye, self._remote_addr)
            self._sip_sock.sendto(bye.encode(), self._remote_addr)
        except Exception as e:
            logger.debug("[RawSIPCall] BYE send error: %s", e)


class SIPServer:
    def __init__(self, agent_id="healthcare", voice="Aoede", language="ur-PK"):
        from .config import (SIP_MODE, SIP_BIND_IP, SIP_BIND_PORT, SIP_SERVER, SIP_SERVER_PORT, SIP_USERNAME, SIP_PASSWORD, SIP_PUBLIC_IP, SIP_TEST_USERNAME, SIP_TEST_PASSWORD, SIP_RTP_PORT_LOW, SIP_RTP_PORT_HIGH, ASTERISK_HOST, ASTERISK_PORT, ASTERISK_USERNAME, ASTERISK_PASSWORD)
        from ._logging import get_local_ip
        from .bridge import SIPCallBridge

        self.agent_id = agent_id
        self.voice    = voice
        self.language = language
        self.mode     = SIP_MODE
        local_ip  = SIP_BIND_IP if SIP_BIND_IP and SIP_BIND_IP != "0.0.0.0" else get_local_ip()
        public_ip = "10.99.39.11"

        if SIP_MODE == "multinet":
            if not SIP_SERVER or not SIP_USERNAME:
                raise ValueError("SIP_SERVER and SIP_USERNAME required for multinet mode.")
            from .registrar import MultinetRegistrar
            self._registrar = MultinetRegistrar(server=SIP_SERVER, port=SIP_SERVER_PORT, username=SIP_USERNAME, password=SIP_PASSWORD, local_ip=local_ip, local_port=SIP_BIND_PORT, public_ip=public_ip, on_registered=self._on_registered, on_failed=self._on_registration_failed)
            self._sip_server = RawSIPServer(bind_ip=local_ip, bind_port=SIP_BIND_PORT, username=SIP_USERNAME, password=SIP_PASSWORD, on_call=self._on_incoming_call, agent_id=agent_id, voice=voice, language=language, rtp_port_low=SIP_RTP_PORT_LOW, rtp_port_high=SIP_RTP_PORT_HIGH)
            self._phone = None
            self._local_ip = local_ip
            self._public_ip = public_ip
            self._bind_port = SIP_BIND_PORT
            self._multinet_addr = SIP_SERVER
            self._multinet_port = SIP_SERVER_PORT
            self._username = SIP_USERNAME
        elif SIP_MODE == "asterisk":
            if not ASTERISK_HOST:
                raise ValueError("ASTERISK_HOST must be set for asterisk mode.")
            from pyVoIP.VoIP import VoIPPhone
            self._phone = VoIPPhone(server=ASTERISK_HOST, port=ASTERISK_PORT, username=ASTERISK_USERNAME, password=ASTERISK_PASSWORD, callCallback=self._on_incoming_call, myIP=local_ip, sipPort=SIP_BIND_PORT, rtpPortLow=SIP_RTP_PORT_LOW, rtpPortHigh=SIP_RTP_PORT_HIGH)
            self._registrar = None
            self._sip_server = None
            self._local_ip = local_ip
            self._bind_port = SIP_BIND_PORT
            self._asterisk_host = ASTERISK_HOST
            self._asterisk_port = ASTERISK_PORT
            self._asterisk_user = ASTERISK_USERNAME
        else:
            self._registrar = None
            self._phone = None
            self._sip_server = RawSIPServer(bind_ip=local_ip, bind_port=SIP_BIND_PORT, username=SIP_TEST_USERNAME, password=SIP_TEST_PASSWORD, on_call=self._on_incoming_call, agent_id=agent_id, voice=voice, language=language, rtp_port_low=SIP_RTP_PORT_LOW, rtp_port_high=SIP_RTP_PORT_HIGH)
            self._local_ip = local_ip
            self._bind_port = SIP_BIND_PORT
            self._test_username = SIP_TEST_USERNAME
            self._test_password = SIP_TEST_PASSWORD
        self._outbound_rtp_counter = SIP_RTP_PORT_HIGH - 100

    def _on_registered(self):
        logger.info("[SIPServer] Registered to Multinet — ready for calls")

    def _on_registration_failed(self):
        logger.error("[SIPServer] Registration failed — incoming calls will NOT arrive")

    def _next_outbound_rtp_port(self):
        port = self._outbound_rtp_counter
        self._outbound_rtp_counter += 2
        if self._outbound_rtp_counter > 20000:
            self._outbound_rtp_counter = 19900
        return port

    def make_outbound_call(self, to_number, local_rtp_port=None):
        if self.mode != "multinet":
            logger.error("[SIPServer] Outbound only supported in multinet mode currently")
            return
        if local_rtp_port is None:
            local_rtp_port = self._next_outbound_rtp_port()
        def on_answered(call):
            bridge = SIPCallBridge(call=call, agent_id=self.agent_id, voice=self.voice, language=self.language)
            bridge.start()
            while bridge._running:
                try:
                    if call.state != CallState.ANSWERED:
                        bridge._running = False
                        break
                except Exception: break
                time.sleep(0.5)
        def on_failed(reason):
            logger.error("[SIPServer] Outbound call failed: %s -> %s", to_number, reason)
        self._registrar.make_call(to_number=to_number, on_answered=on_answered, on_failed=on_failed, local_rtp_port=local_rtp_port, agent_id=self.agent_id, voice=self.voice, language=self.language)

    def start(self):
        if self.mode == "multinet":
            self._registrar.start()
            for i in range(15):
                if self._registrar.registered: break
                time.sleep(1)
            self._sip_server._shared_sock = self._registrar.get_socket()
            self._sip_server.start()
        elif self.mode == "asterisk":
            self._phone.start()
        else:
            self._sip_server.start()

    def stop(self):
        if self.mode == "asterisk" and self._phone:
            self._phone.stop()
        else:
            if self._sip_server: self._sip_server.stop()
            if self._registrar: self._registrar.stop()

    def _on_incoming_call(self, call):
        from .bridge import SIPCallBridge
        caller = getattr(call, "caller", "unknown")
        try:
            call.answer()
            bridge = SIPCallBridge(call=call, agent_id=self.agent_id, voice=self.voice, language=self.language)
            bridge.start()
            while bridge._running:
                try:
                    if call.state != CallState.ANSWERED:
                        bridge._running = False
                        break
                except Exception: break
                time.sleep(0.5)
        except InvalidStateError: pass
        except Exception as e:
            logger.error("[SIPServer] Call error: %s", e, exc_info=True)
            try: call.hangup()
            except Exception: pass
