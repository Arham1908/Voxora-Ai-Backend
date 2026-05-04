## WhatsApp Call — Graceful End Implementation

Added **graceful call termination** for WhatsApp WebRTC calls when tasks complete or goodbye is detected.

### Problem Fixed

Before: When a call ended, it could be abrupt or prolonged. Tasks didn't have a clean way to signal completion to end the call naturally.

Now: ✅ Calls end gracefully when agent says goodbye or completes terminal tasks.

### Implementation

**1️⃣ WebSocketBridge Enhanced** (`whatsapp/calls.py:152-227`)
```python
class WebSocketBridge:
    def set_on_call_end(self, callback):
        """Set callback when agent signals task complete."""
        self._on_call_end = callback

    async def _listen_agent(self):
        # Listen for JSON control messages
        if event == "call_end":
            reason = data.get("reason", "task_complete")
            if self._on_call_end:
                await self._on_call_end(reason)  # Callback triggered
```

**2️⃣ WhatsAppCallSession Handles Graceful End** (`whatsapp/calls.py:406-429`)
```python
async def on_agent_call_end(reason: str):
    """When agent signals end, terminate gracefully."""
    await end_call_api(self.call_id)  # Notify Meta API
    await self.cleanup()               # Clean up WebRTC + streams
```

**3️⃣ Consumer Detects Goodbye + Signals End** (`voice/consumers.py:1075-1103`)
```python
if goodbye_detected:  # Agent said "Allah Hafiz" or "Goodbye"
    # Signal end to WhatsApp/external call
    await self.send(text_data=json.dumps({
        "event": "call_end",
        "reason": "goodbye_detected"
    }))
    self._should_end_call = True
```

### Call Flow — Graceful End

```
Agent finishes task → Detects "Allah Hafiz" in response
     ↓
BrowserVoiceConsumer detects goodbye phrase
     ↓
Sends JSON: {"event": "call_end", "reason": "goodbye_detected"}
     ↓
WebSocketBridge._listen_agent() receives it
     ↓
Invokes on_agent_call_end callback
     ↓
Calls end_call_api(call_id) → notifies Meta
     ↓
Calls cleanup() → closes WebRTC peer connection
     ↓
WhatsApp caller hears hangup tone (graceful)
```

### Goodbye Phrases Detected

Current detection keywords:
- "allah hafiz" (اللہ حافظ)
- "khuda hafiz"
- "goodbye"
- "bye"

Urdu phrases work both in Urdu script + Roman Urdu:
- "اللہ حافظ" ✅
- "Allah Hafiz" ✅
- "Khuda Hafiz" ✅

### Terminal Tasks

When completing these tasks, agent naturally says goodbye, triggering graceful end:
- ✅ `book_appointment` — after confirmation
- ✅ `place_order` — after order confirmed
- Any response containing goodbye phrase

### Behavior

| Event | Response |
|---|---|
| Agent says "Allah Hafiz" | Call ends gracefully in 6 seconds |
| WebRTC connection dies | Auto-cleanup triggered |
| Meta API signals "terminate" | Cleanup immediately |
| Task completed (appointment booked) | Agent says goodbye → call ends |

### 6-Second Delay After Goodbye

```python
asyncio.create_task(self._delayed_close(6.0))
```

Why 6 seconds?
- Gives agent time to finish last phrase
- Allows final audio to reach caller
- Natural break before hangup
- Matches WebRTC close timeout

### Files Modified

- `whatsapp/calls.py` — WebSocketBridge + WhatsAppCallSession enhancements
- `voice/consumers.py` — Goodbye detection + call_end signal

### Testing

```bash
# 1. WhatsApp call incoming → agent connects
# 2. User books appointment
# 3. Agent says "Humein call karne ka shukriya! Allah Hafiz!"
# 4. Consumer detects goodbye → sends call_end event
# 5. WhatsAppCallSession.cleanup() triggered
# 6. Meta API called with terminate action
# 7. Call ends cleanly (not abrupt, not prolonged)
```

### Logging

When graceful end triggers, you'll see:

```
[call_id] Agent signaled end: goodbye_detected — closing call gracefully
[call_id] Call terminated via API
[call_id] Call session cleaned up
```

### Future Enhancements

- [ ] Configurable goodbye phrases per agent
- [ ] Custom delay per call type
- [ ] Post-call voicemail option
- [ ] Call recording stopped before cleanup
- [ ] Analytics on goodbye reason (task_complete vs. user_initiated)
