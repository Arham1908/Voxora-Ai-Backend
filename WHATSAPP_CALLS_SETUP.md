# WhatsApp Calling API Integration (WebRTC Bridge)

This integration enables WhatsApp Business Calling on your app, routing incoming calls to your existing Sara/Zara Gemini Live voice agents via Django Channels WebSockets.

## What's New

- **`whatsapp/calls.py`** — WebRTC bridge logic using aiortc
- **`whatsapp/meta_views.py`** — New `meta_call_webhook()` and `meta_call_health()` views
- **`whatsapp/urls.py`** — New call webhook routes

## Setup

### 1. Environment Variables

Add to your `.env`:

```bash
# Existing Meta config (messages)
META_VERIFY_TOKEN=your_verify_token
META_ACCESS_TOKEN=your_permanent_token
META_PHONE_NUMBER_ID=your_phone_id

# New: Agent WebSocket URLs (Django Channels)
AGENT_SARA_WS_URL=ws://localhost/ws/voice/sara/
AGENT_ZARA_WS_URL=ws://localhost/ws/voice/zara/

# Optional: Override for production
WHATSAPP_ACCESS_TOKEN=your_permanent_token  # Falls back to META_ACCESS_TOKEN
WHATSAPP_VERIFY_TOKEN=your_verify_token      # Falls back to META_VERIFY_TOKEN
```

### 2. Meta Dashboard Configuration

1. Go to **Meta App Dashboard** → Your app → **WhatsApp** → **Configuration**
2. Add webhook subscriptions:
   - **Messages**: `https://yourdomain.com/whatsapp/meta/webhook/`
   - **Calls**: `https://yourdomain.com/whatsapp/meta/calls/webhook/`
3. Both use the same `VERIFY_TOKEN`

### 3. Firewall / Network Rules

- WhatsApp WebRTC calls require **bidirectional UDP** ports (typically 49152-65535)
- Ensure your firewall allows UDP traffic for aiortc
- Test: `ping yourdomain.com` and check logs for ICE candidate errors

## How It Works

```
WhatsApp User calls → Meta sends SDP offer
   ↓
meta_call_webhook() receives event
   ↓
handle_call_event() creates WhatsAppCallSession
   ↓
aiortc negotiates WebRTC connection ↔ WhatsApp
   ↓
Django Channels consumer (Sara/Zara) starts
   ↓
Audio pipes in both directions:
   - Caller audio → agent input
   - Agent output → caller audio
   ↓
Call ends → cleanup
```

## Testing

### Health Check

```bash
curl http://localhost:8000/whatsapp/meta/calls/health/
```

Response:
```json
{
  "status": "active",
  "service": "BlenSpark Meta WhatsApp Calls (WebRTC)",
  "active_calls": 1,
  "calls": [
    {
      "call_id": "...",
      "caller": "+923001234567",
      "agent": "sara",
      "webrtc_state": "connected"
    }
  ]
}
```

### Manual Webhook Test

Use `meta_call_webhook.py` test script or `ngrok` to expose local dev environment:

```bash
# Terminal 1: Start Django
python manage.py runserver

# Terminal 2: Expose to internet
ngrok http 8000

# Terminal 3: Send test webhook
curl -X POST http://localhost:8000/whatsapp/meta/calls/webhook/ \
  -H "Content-Type: application/json" \
  -d '{
    "entry": [{
      "changes": [{
        "field": "calls",
        "value": {
          "call_id": "test_call_123",
          "status": "initiated",
          "from": "+923001234567",
          "sdp": "v=0\no=..."
        }
      }]
    }]
  }'
```

## Logs

Monitor logs for:
- `📞 Call event` — incoming call detected
- `[call_id] Connected to agent` — WebSocket to Sara/Zara established
- `[call_id] WebRTC state: connected` — audio flowing
- `[call_id] Call session cleaned up` — call ended

```bash
# Django logs
tail -f logs/django.log | grep "Call event"
```

## Troubleshooting

### ❌ "Agent connect failed"
- Check `AGENT_SARA_WS_URL` is correct
- Verify Django Channels consumer is running
- Test: `wscat -c ws://localhost/ws/voice/sara/`

### ❌ "SDP answer failed"
- `META_ACCESS_TOKEN` invalid or expired
- Network connectivity to Graph API blocked
- Check Meta dashboard error logs

### ❌ "Audio forward error"
- aiortc format mismatch — verify 16kHz PCM mono
- Consumer not accepting binary frames
- Check consumer logging for frame size errors

### ❌ "No audio in call"
- Caller's codec not supported (should be opus)
- Consumer not pushing audio to `audio_sender.push_audio()`
- WebRTC connection state still "connecting" (wait 1-2s)

## Architecture Notes

### Audio Format
- **Input (WhatsApp → Agent)**: 16-bit PCM, 16kHz mono, sent as binary frames
- **Output (Agent → WhatsApp)**: Same format, queued via `AgentAudioSender`

### WebRTC Codec
- **Offer from WhatsApp**: Typically Opus (required for WhatsApp)
- **Answer from aiortc**: Auto-matches caller codec

### Fallback Behavior
- If call fails, automatically end call and notify caller
- If agent WebSocket drops, close WebRTC connection
- If audio conversion fails, log error but continue (will hear silence)

## Production Deployment

1. **Use HTTPS only** — ngrok/tunneling for dev, reverse proxy for production
2. **Environment secrets** — store tokens in Railway/Vercel/Heroku secrets, not `.env`
3. **Load balancing** — `active_calls` dict is in-memory; use Redis for multi-worker setups
4. **Monitoring** — log `call_id` in all operations; track `active_calls` count via `/health/`
5. **Timeout tuning** — adjust aiohttp timeouts if Meta is slow (adjust `timeout=15` in `calls.py`)

## Future Enhancements

- [ ] Call recording (enable `MediaRecorder` in aiortc)
- [ ] Call transfer between agents (Zara → Sara)
- [ ] Call history tracking to database
- [ ] ICE candidate buffering for slow networks
- [ ] Graceful call handoff on deployment

---

**Reference**: [aiortc docs](https://aiortc.readthedocs.io/) | [Meta Calling API](https://developers.facebook.com/docs/whatsapp/business-platform-for-developer/phone-calling)
