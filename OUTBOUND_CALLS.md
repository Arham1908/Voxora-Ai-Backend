## Outbound Calls — Implementation Summary

Added complete **outbound call** support to BlenSpark Voice Agent. No more inbound-only limitation!

### What's New

**MultinetRegistrar** (in `sip_client.py`):
- `make_call()` — async outbound call method
- `_do_invite()` — full SIP INVITE flow (with auth retry on 407/401)
- `_build_invite_sdp()` — builds SDP for outbound
- `_build_digest_auth_invite()` — RFC 3261 digest for INVITE (URI = target, not REGISTER)
- `_parse_sdp_rtp_from_200()` — extracts remote RTP IP:port from 200 OK
- **`_outbound_sip_listener()`** — listens for remote BYE on outbound socket + replies 200 OK
- Auto BYE listener spawned after ACK

**SIPServer**:
- `make_outbound_call(to_number)` — exposed public method (auto-assigns RTP port)
- **`_next_outbound_rtp_port()`** — auto-increment port counter for concurrent calls
- Handles port allocation starting from 19900

**RawSIPCall**:
- **RTP silence timeout (30s)** — detects dead calls, ends automatically if no RTP received

**Socket Handling**:
- ✅ Uses **separate UDP socket** for outbound to avoid conflicts with registration loop
- ✅ No blocking during REGISTER interferes with INVITE responses

### Production Fixes Applied

| Issue | Fix | Status |
|---|---|---|
| Remote BYE never handled | `_outbound_sip_listener()` + daemon thread | ✅ Fixed |
| RTP port hardcoded → concurrent conflicts | `_next_outbound_rtp_port()` auto-assign | ✅ Fixed |
| RTP silence → infinite calls | 30s silence timeout detection | ✅ Fixed |

### Usage — Command Line

```bash
# Basic call (auto RTP port)
python manage.py make_call +923001234567

# With custom agent/voice/language
python manage.py make_call +923001234567 --agent healthcare --voice Aoede --language ur-PK

# With 33 RTP port (optional override)
python manage.py make_call +923001234567 --rtp-port 12003
```

### Usage — Python Code

```python
from voice.sip_client import SIPServer

server = SIPServer(agent_id="healthcare", voice="Aoede", language="ur-PK")
server.start()

# Make outbound call (port auto-assigned)
server.make_outbound_call("+923001234567")

# Make another — different port automatically
server.make_outbound_call("+923007654321")

# Keep running...
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    server.stop()
```

### Call Flow (With Fixes)

```
You (Python)
    ↓ INVITE
    ↓ (separate socket)
Multinet SIP
    ↓ (407/401 or 200 OK)
    ↓ (via separate socket)
You (Python)
    ↓ ACK
    ↓ Spawn BYE listener (daemon)
Multinet
    ↓ RTP starts (timeout monitoring active)
    ↓
You → Gemini Live Bridge (same as inbound)
    ↓
Gemini AI Agent
    ↓
(30s silence timeout OR remote BYE) → call ends automatically
```

### Key Improvements Over Initial Version

1. **BYE Handling** — Remote party hangs up? Gets 200 OK + call ends immediately (not zombie)
2. **Port Management** — Concurrent calls automatically use different RTP ports
3. **Silent Call Detection** — Network drops detected within 30s instead of hanging forever
4. **Thread-Safe** — All operations non-blocking

### Files Modified

- `voice/sip_client.py` — MultinetRegistrar + SIPServer + RawSIPCall enhancements
- `voice/management/commands/make_call.py` — management command
- `OUTBOUND_CALLS.md` — this documentation

### Testing

```bash
# Start server & make outbound call
python manage.py make_call +923001234567

# Or in Python shell with concurrent calls
python manage.py shell
>>> from voice.sip_client import SIPServer
>>> s = SIPServer()
>>> s.start()
>>> s.make_outbound_call("+923001234567")  # RTP port auto-assigned (e.g. 19900)
>>> s.make_outbound_call("+923007654321")  # Different RTP port (e.g. 19902)
```

### Multinet Mode Only (For Now)

Outbound currently only works in `SIP_MODE=multinet`. Support for asterisk/local modes can be added later if needed.

### Behavior Details

**BYE Listener**:
- Runs as daemon thread on same socket as INVITE/ACK
- Listens for 30 seconds (or until call ends)
- Replies 200 OK to remote BYE immediately
- Stops call state + wakes bridge

**RTP Silence Timeout**:
- Starts counting from first packet
- Resets on each received RTP packet
- If 30s passes without data → call ends automatically
- Logs warning when triggered

**Port Auto-Assignment**:
- Starts at `SIP_RTP_PORT_HIGH - 100` (default 19900)
- Increments by 2 per call
- Wraps around at 20000
- Manual override still works: `make_outbound_call(num, local_rtp_port=12345)`

### Next Steps (Optional)

- [ ] Add call status tracking (dialing/ringing/answered/ended)
- [ ] Add hangup-from-Python support (not just remote BYE)
- [ ] Extend to asterisk/local modes
- [ ] Add call logging to DB (like inbound)
- [ ] Configurable silence timeout per call

