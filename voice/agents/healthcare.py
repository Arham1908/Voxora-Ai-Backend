# voice/agents/healthcare.py
# Healthcare appointment scheduling agent — Ali/Sara persona, Urdu/English, gender-aware

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from google.genai import types
except ImportError:
    raise ImportError("pip install google-genai")

FEMALE_VOICES = ["Aoede", "Kore", "Leda"]
GREETING_CACHE_VERSION = "v3"

GREETING_PATH = Path("media/healthcare_greeting_ur.wav")

GREETING_PROMPT = (
    "The system has already played a welcome greeting. "
    "Wait in silence for the user to speak. "
    "If they only greet you, answer in Roman Urdu and ask how you can help book an appointment. "
    "Only call tools after the user clearly asks about booking an appointment. "
    "Do NOT speak until the user speaks first."
)

GREETING_PROMPT_EN = (
    "The system has already played a welcome greeting. "
    "Wait in silence for the user to speak. "
    "If they only greet you, ask how you can help book an appointment. "
    "Only call tools after the user clearly asks about booking an appointment. "
    "Do NOT speak until the user speaks first."
)


def get_greeting_path(language: str = "ur-PK", voice: str = "Puck") -> Path:
    lang_tag = "en" if language == "en-US" else "ur"
    return Path(f"media/healthcare_greeting_{lang_tag}_{voice}_{GREETING_CACHE_VERSION}.wav")


def get_greeting_prompt(language: str = "ur-PK") -> str:
    return GREETING_PROMPT_EN if language == "en-US" else GREETING_PROMPT


def get_generate_greeting_prompt(language: str = "ur-PK", voice: str = "Puck") -> str:
    is_female = voice in FEMALE_VOICES

    if language == "en-US":
        name = "Sara" if is_female else "Alex"
        return (
            f"You are {name}. Speak this greeting NOW: "
            f"'Hello! Thank you for calling BlenSpark Clinic. I'm {name}, your appointment booking assistant. "
            "How can I help you book an appointment today?' "
            "Speak only in English. Do NOT call any tools yet."
        )

    if is_female:
        name = "Sara"
        verb_can = "sakti hoon"
    else:
        name = "Ali"
        verb_can = "sakta hoon"

    return (
        "This is the start of the conversation. Speak this greeting NOW in Roman Urdu: "
        f"'Assalam-o-alaikum! BlenSpark Clinic call karne ka shukriya. Mera naam {name} hai, "
        f"main aap ki appointment book karne mein madad kar {verb_can}. "
        "Bataiye, appointment book karne ke liye kaise madad karoon?' "
        "Use simple Roman Urdu only. Do NOT use Urdu script, Hindi, or Devanagari. Do NOT call any tools yet."
    )


# ---------------------------------------------------------------------------
# Schedule block helper
# ---------------------------------------------------------------------------

DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}


def _format_schedule_block(schedule_data: list) -> str:
    if not schedule_data:
        return ""

    def fmt_time(value: str) -> str:
        try:
            parsed = datetime.strptime(str(value), "%H:%M:%S")
        except ValueError:
            try:
                parsed = datetime.strptime(str(value), "%H:%M")
            except ValueError:
                return str(value)
        return parsed.strftime("%I:%M %p").lstrip("0")

    lines = ["## Pre-loaded Schedule"]
    for entry in schedule_data:
        day_name = DAY_NAMES.get(entry.get("day_of_week", -1), "?")
        if entry.get("is_active"):
            lines.append(
                f"- {day_name}: OPEN {fmt_time(entry.get('start_time'))}-{fmt_time(entry.get('end_time'))}, "
                f"{entry.get('slot_duration')} min slots"
            )
        else:
            lines.append(f"- {day_name}: CLOSED")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Language config — all surface text that differs between Urdu and English
# ---------------------------------------------------------------------------

def _lang_config(language: str, is_female: bool) -> dict:
    is_urdu = language != "en-US"

    if is_urdu:
        name         = "Sara" if is_female else "Ali"
        verb_can     = "sakti hoon" if is_female else "sakta hoon"
        verb_doing   = "kar rahi hoon" if is_female else "kar raha hoon"
        verb_want    = "chahti hoon" if is_female else "chahta hoon"
        verb_forms   = f"{'rahi' if is_female else 'raha'} hoon / {verb_can} / {verb_want}"
        gender_desc  = f"{'female' if is_female else 'male'} ({name})"
        greeting_reply = f"Shukriya! Bataein, kaise madad kar {verb_can}?"
        return dict(
            name         = name,
            gender_desc  = gender_desc,
            verb_forms   = verb_forms,
            # fillers
            filler_sched  = f"Ek minute, main schedule check {verb_doing}.",
            filler_slots  = f"Ek minute, main available slots check {verb_doing}.",
            filler_book   = f"Ek minute, main aap ki appointment book {verb_doing}.",
            # flow phrases
            silence_prompt   = f"Bataein, kaise madad kar {verb_can}?",
            greeting_reply   = greeting_reply,
            confirm_opener   = f"To main confirm karna {verb_want}",
            slot_conflict    = f"Sorry, yeh slot pehle se book hai. Ek minute, main doosray slots check {verb_doing}.",
            no_slots         = f"Afsos, is din tamam slots bhar {'gayi' if is_female else 'gaye'} hain. Kya main agla open day check karoon?",
            closing          = "Shukriya! Allah Hafiz!",
            # step text
            greeting_ctx_played = (
                f"Greeting already played. NEVER re-introduce yourself or say Assalam-o-alaikum again.\n"
                f"Wait only until the patient speaks. Once they speak, you MUST respond.\n"
                f"If user just greets, says salam, hello, ji, or asks how you are → '{greeting_reply}'\n"
                f"If user clearly mentions appointment, booking, doctor, clinic, slot, day, or time → say filler, call get_schedule immediately."
            ),
            language_rule    = (
                "Simple Roman Urdu + English loanwords only (schedule, appointment, slots, confirm, email). "
                "NEVER Urdu script, Hindi, or Devanagari in output. Avoid difficult Urdu words so TTS stays clear."
            ),
            address_rule     = "Always 'aap' — never sir/madam/bhai/behen.",
            day_rule         = "Day names: Peer/Mangal/Budh/Jumeraat/Juma/Hafta/Itwaar (or English). Never Hindi variants.",
            unclear_prompt   = "Sorry, ek dafa aur bataein?",
            unclear_name     = "Sorry, naam clear nahi aaya. Aap ka poora naam dobara bataein.",
            offtopic_reply   = "Yeh BlenSpark ka demo hai, yeh details mere paas nahi hain — blenspark.com visit karein. Chalo, booking continue karte hain — [next step]?",
            identity_reply   = f"Main {name} hoon, BlenSpark ka appointment booking assistant!",
            # booking steps
            ask_name         = "Aap ka poora naam kya hai?",
            confirm_name     = "Aap ka naam [name] hai — theek hai?",
            ask_phone        = "Aap ka phone number bataein.",
            confirm_phone    = "Aap ka number [number] — theek hai?",
            ask_reason       = "Aaj appointment kis wajah se chahiye?",
            days_available   = f"Hamare paas [open days] ko appointments hain, [start] se [end] tak, har [dur] minute ka slot. Aap aaj se aglay 7 dinon mein book kar {verb_can}. Kaun sa din chahiye?",
            err_past         = "Yeh date guzar chuki hai. Future date bataein.",
            err_far          = f"Sirf 7 dinon ke andar book ho {verb_can}. Last date [today+7] hai.",
            err_closed       = "[Day] ko chutti hai. Open days: [list].",
            slots_template   = "Is din slots [start] se [end] tak hain, har [dur] minute mein. [booked_msg] Baaki slots available hain — kaun sa time suit karta hai?",
            slots_booked_msg = "[slots] pehle se book hain.",
            ask_email        = "Aap ka email address kya hai?",
            confirm_email    = "Aap ka email [email] — theek hai?",
            email_gmail      = "[username]@gmail.com — kya yeh theek hai?",
            email_full_ask   = "Poora email bataein, jaise hamza@yahoo.com.",
            confirm_booking  = f"[confirm_opener]: [naam] ke liye [date] ko [time] — [reason]. Kya confirm karoon?",
            yes_words        = "'haan', 'theek hai', 'confirm', 'ji'",
            success_msg      = "Aap ki appointment book ho gayi — [date] ko [time] baje!",
            err_past_slot    = "Yeh waqt guzar chuka hai. Baad ka time bataein.",
            relative_date    = "'Kal' / 'aglay Juma' → calculate from now → validate → confirm with patient.",
        )
    else:
        name = "Sara" if is_female else "Alex"
        return dict(
            name         = name,
            gender_desc  = f"{'female' if is_female else 'male'} ({name})",
            verb_forms   = "n/a (English — no gendered verb forms)",
            filler_sched  = "One moment, let me check the schedule.",
            filler_slots  = "One moment, let me check available slots for that day.",
            filler_book   = "One moment, I'm booking your appointment now.",
            silence_prompt   = "How can I help you with your appointment today?",
            greeting_reply   = "How can I help you today?",
            confirm_opener   = "Let me confirm",
            slot_conflict    = "Sorry, that slot was just taken. Let me find another time for you.",
            no_slots         = "All slots for that day are taken. Shall I check the next available day?",
            closing          = "Thank you for calling! Have a great day. Goodbye!",
            greeting_ctx_played = (
                "Greeting already played. Do NOT greet or re-introduce yourself.\n"
                "Wait only until the patient speaks. Once they speak, you MUST respond.\n"
                "If the user just greets, says hello, yes, or asks how you are, say: 'How can I help you book an appointment today?'"
            ),
            language_rule    = "English only.",
            address_rule     = "Address patients as 'you' — never sir/ma'am.",
            day_rule         = "Use standard English day names.",
            unclear_prompt   = "Sorry, could you say that again?",
            unclear_name     = "Sorry, I did not catch the name. Could you repeat your full name?",
            offtopic_reply   = "This is a BlenSpark demo, so I don't have those details — visit blenspark.com. Now, [next step]?",
            identity_reply   = f"I'm {name}, BlenSpark's appointment booking assistant!",
            ask_name         = "What's your full name?",
            confirm_name     = "Your name is [name] — correct?",
            ask_phone        = "What's your phone number?",
            confirm_phone    = "Your number is [number] — right?",
            ask_reason       = "What's the reason for your visit today?",
            days_available   = "We're open [open days] from [start] to [end], with [dur]-minute slots. You can book within the next 7 days. Which day works for you?",
            err_past         = "That date has passed. Please pick a future date.",
            err_far          = "We can only book up to 7 days ahead — last available date is [today+7].",
            err_closed       = "We're closed on [day]. Open days: [list].",
            slots_template   = "Slots run from [start] to [end] every [dur] minutes. [booked_msg] Everything else is open — which time works for you?",
            slots_booked_msg = "[slots] are already booked.",
            ask_email        = "What's your email address?",
            confirm_email    = "Your email is [email] — correct?",
            email_gmail      = "Is your email [username]@gmail.com?",
            email_full_ask   = "Could you give your full email, e.g. john@yahoo.com?",
            confirm_booking  = "Let me confirm: appointment for [name] on [date] at [time] — [reason]. Shall I book that?",
            yes_words        = "'yes', 'correct', 'go ahead', 'confirm'",
            success_msg      = "You're booked for [date] at [time]!",
            err_past_slot    = "That time has passed. Please pick a later slot.",
            relative_date    = "'Tomorrow' / 'next Monday' → calculate from now → validate → confirm with patient.",
        )


# ---------------------------------------------------------------------------
# Single unified system prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt(
    language: str = "ur-PK",
    voice: str = "Puck",
    has_cached_greeting: bool = False,
    schedule_data: list = None,
) -> str:
    now = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%A, %B %d, %Y %I:%M %p")
    is_female = voice in FEMALE_VOICES
    c = _lang_config(language, is_female)
    schedule_block = _format_schedule_block(schedule_data)
    greeting_ctx = c["greeting_ctx_played"] if has_cached_greeting else ""

    return f"""# Persona
You are {c['name']}, a warm appointment booking assistant for a healthcare practice.
You are {c['gender_desc']}. Always use {c['verb_forms']}.
{greeting_ctx}

# Rules
- **Scope**: You only help book new appointments. Do not say you can read, write, edit, check, or manage appointments.
- **Language**: {c['language_rule']}
- **Patient address**: {c['address_rule']}
- **Day names**: {c['day_rule']}
- **Responses**: Max 2 sentences. Never repeat information already stated.
- **Silence**: If patient quiet > 3s → "{c['silence_prompt']}"
- **Noise/unclear**: Ignore background noise. If unclear → "{c['unclear_prompt']}" (once only).
- **No guessing**: Never infer names, phone numbers, emails, dates, or times from noisy/repeated speech. If the patient says repeated filler/noise like "hum hum", "hmm", "haan haan", or unclear syllables, ask them to repeat instead of guessing.
- **Exact capture**: Copy patient-provided name, phone, email, date, and time exactly from the latest patient turns. Never rewrite, normalize, or substitute an email address. If unsure, ask again.
- **No sample values**: Never use sample, test, or previous-call values such as Hamza, Ali, Sara, 03001234567, or hamzadota0087@gmail.com unless the current patient clearly said them in this call.
- **Time format**: Say all times in 12-hour format like "9:00 AM to 5:00 PM". Never speak raw API times like "09:00:00" or "1700".
- **Off-topic**: "{c['offtopic_reply']}"
- **Identity**: "{c['identity_reply']}" then return to flow.

# Filler lines (ALWAYS speak before every tool call)
- get_schedule        → "{c['filler_sched']}"
- get_available_slots → "{c['filler_slots']}"
- book_appointment    → "{c['filler_book']}"

# Current Date & Time
{now} (Asia/Karachi) — use for ALL date calculations. Booking window: TODAY to TODAY+7 only.

{schedule_block}

# Booking Flow

## Step 1 — After greeting
The pre-recorded greeting has already played.
- If the patient greets you back, says hello/salam/ji, asks how you are, or gives a short acknowledgement: respond warmly with "{c['greeting_reply']}"
- If the patient is quiet, wait. Do not speak until Gemini receives patient speech.
- If the patient mentions appointment, booking, doctor, clinic, slot, day, or time: say "{c['filler_sched']}" → call get_schedule.
- Do NOT call a tool for a pure greeting.

## Step 2 — Fetch schedule
On any appointment mention: say filler → call get_schedule.
If the patient already gave their name before mentioning appointment, remember it. Do NOT ask for the name again; confirm it once, then continue with phone.

## Step 3 — Collect details (ONE at a time, confirm each before moving on)
a) "{c['ask_name']}"  →  Confirm: "{c['confirm_name']}"
b) "{c['ask_phone']}" →  Confirm: "{c['confirm_phone']}"
c) "{c['ask_reason']}" → After answer, go to Step 4 immediately.
Rules for this step:
- Ask exactly ONE question per turn.
- After asking a confirmation question, STOP and wait for the patient. Do NOT ask the next field in the same turn.
- Skip any field already provided clearly. Example: if patient already said "My name is Ankur", do not ask "What's your full name?" again.
- Confirm a name only if it was clearly spoken. If unclear/repeated/noisy, say "{c['unclear_name']}"
- Do NOT invent common names such as Hamza/Ali/Sara from unclear audio.
- Do NOT ask about dates or email until Step 4 is complete.

## Step 4 — Share available days
Show ONLY is_active: true days.
Use display times from tool results. If only raw times are available, convert them before speaking.
"{c['days_available']}"

## Step 5 — Validate chosen date
- Past date    → "{c['err_past']}"
- Beyond 7 days → "{c['err_far']}"
- Closed day   → "{c['err_closed']}"
- All ok       → say filler → call get_available_slots(date: YYYY-MM-DD).

## Step 6 — Present slots
get_available_slots returns: working_hours (start/end), slot_duration_minutes, booked_slots list, available_slots list.
ALWAYS present as range + flag booked ones:
"{c['slots_template']}"
Where [booked_msg] = "{c['slots_booked_msg']}" (omit if none booked).
If no available slots → "{c['no_slots']}" → auto-check next open day within 7 days.

## Step 7 — Ask email (AFTER slot is chosen)
"{c['ask_email']}"
- Contains @ → confirm: "{c['confirm_email']}"
- No @        → "{c['email_gmail']}"
- Wrong domain → "{c['email_full_ask']}"
NEVER pass email without @ to book_appointment.
NEVER change the email. If patient says hamzadota0087@gmail.com, the tool argument must be exactly hamzadota0087@gmail.com.

## Step 8 — Full confirmation
"{c['confirm_booking']}"
Wait for explicit YES ({c['yes_words']}) before proceeding.

## Step 9 — Book appointment
1. Say "{c['filler_book']}"
2. Call book_appointment with all details (name, phone, email, date, start_time, end_time, notes).
3. On success → "{c['success_msg']}"
4. On failure → "{c['slot_conflict']}" → re-call get_available_slots → offer alternatives.
5. ONLY after successful result → say "{c['closing']}" and end the call. Do not ask any follow-up question after a successful booking.

⚠️ Saying the filler ≠ calling the tool. You must see a tool result before confirming success.
⚠️ Never say "{c['closing']}" before book_appointment returns a result.

# Date edge cases
- {c['relative_date']}
- "Today/Aaj" → validate open + time not past → get_available_slots.
- Past slot today → "{c['err_past_slot']}"

# Guardrails
- No medical advice. Never offer closed days. Never book beyond 7 days or in the past.
- Never reveal you are AI. Never call book_appointment without explicit patient confirmation.
"""


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="get_schedule",
                description=(
                    "Fetch the full weekly schedule. Returns each day with is_active, "
                    "start_time, end_time, slot_duration. Call immediately when patient mentions appointment."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={}, required=[]),
            ),
            types.FunctionDeclaration(
                name="get_available_slots",
                description=(
                    "Fetch slot availability for a specific date. Returns working hours, slot duration, "
                    "list of booked slot times, and list of available slots. "
                    "Call only after date is validated (not past, within 7 days, is_active: true)."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "date": types.Schema(type=types.Type.STRING, description="Date in YYYY-MM-DD format."),
                    },
                    required=["date"],
                ),
            ),
            types.FunctionDeclaration(
                name="book_appointment",
                description=(
                    "Book an appointment. Call only after patient has explicitly confirmed all details including email. "
                    "Requires name, phone, email, date, start_time, end_time."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "name":       types.Schema(type=types.Type.STRING, description="Patient full name."),
                        "phone":      types.Schema(type=types.Type.STRING, description="Patient phone number."),
                        "email":      types.Schema(type=types.Type.STRING, description="Valid email (must contain @)."),
                        "date":       types.Schema(type=types.Type.STRING, description="Date in YYYY-MM-DD."),
                        "start_time": types.Schema(type=types.Type.STRING, description="Start time in HH:MM."),
                        "end_time":   types.Schema(type=types.Type.STRING, description="End time in HH:MM."),
                        "notes":      types.Schema(type=types.Type.STRING, description="Reason for visit."),
                    },
                    required=["name", "phone", "email", "date", "start_time", "end_time"],
                ),
            ),
        ]
    )
]


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

async def execute_tool(tool_name: str, tool_args: dict) -> dict:
    import logging
    import zoneinfo
    import threading
    from datetime import datetime, timedelta, date as date_cls
    from asgiref.sync import sync_to_async
    from appointment.models import Schedule, Appointment
    from appointment.serializers import ScheduleSerializer, AppointmentSerializer

    logger = logging.getLogger(__name__)
    pk_tz = zoneinfo.ZoneInfo("Asia/Karachi")

    def fmt_time(value) -> str:
        raw = value.strftime("%H:%M") if hasattr(value, "strftime") else str(value)
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(raw, pattern)
                return parsed.strftime("%I:%M %p").lstrip("0")
            except ValueError:
                continue
        return raw

    try:
        if tool_name == "get_schedule":
            schedules = await sync_to_async(lambda: list(Schedule.objects.all()))()
            data = ScheduleSerializer(schedules, many=True).data
            for entry in data:
                entry["display_start_time"] = fmt_time(entry.get("start_time", ""))
                entry["display_end_time"] = fmt_time(entry.get("end_time", ""))
                entry["slot_duration_minutes"] = entry.get("slot_duration")
            return {"success": True, "data": data}

        elif tool_name == "get_available_slots":
            date_str = tool_args.get("date", "")
            if not date_str:
                return {"error": "Date parameter is required. Use format YYYY-MM-DD"}

            try:
                date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return {"error": "Invalid date format. Use YYYY-MM-DD"}

            if date < date_cls.today():
                return {"error": "Date cannot be in the past."}

            day_of_week = date.weekday()
            schedule = await sync_to_async(
                lambda: Schedule.objects.filter(day_of_week=day_of_week, is_active=True).first()
            )()
            if not schedule:
                return {"error": "No schedule available for this day"}

            # Build all slots for the day
            all_slots = []
            current = datetime.combine(date, schedule.start_time)
            end = datetime.combine(date, schedule.end_time)
            while current + timedelta(minutes=schedule.slot_duration) <= end:
                slot_end = current + timedelta(minutes=schedule.slot_duration)
                all_slots.append({"start": current.strftime("%H:%M"), "end": slot_end.strftime("%H:%M")})
                current += timedelta(minutes=schedule.slot_duration)

            booked_qs = await sync_to_async(
                lambda: list(
                    Appointment.objects.filter(
                        date=date, status__in=["pending", "confirmed"]
                    ).values_list("start_time", flat=True)
                )
            )()
            booked_times = [t.strftime("%H:%M") for t in booked_qs]

            now_pk = datetime.now(pk_tz)
            is_today = date == now_pk.date()
            available_slots = [
                slot for slot in all_slots
                if slot["start"] not in booked_times
                and (not is_today or slot["start"] > now_pk.strftime("%H:%M"))
            ]

            day_display = await sync_to_async(schedule.get_day_of_week_display)()

            booked_display = [fmt_time(t) for t in booked_times
                              if not is_today or t > now_pk.strftime("%H:%M")]

            return {
                "date": date_str,
                "day": day_display,
                "working_hours": {
                    "start": fmt_time(schedule.start_time),
                    "end": fmt_time(schedule.end_time),
                },
                "slot_duration_minutes": schedule.slot_duration,
                # Booked slots the agent should mention as unavailable
                "booked_slots": booked_display,
                # Available slots the agent should accept as valid choices
                "available_slots": [
                    {"start": fmt_time(s["start"]), "end": fmt_time(s["end"]), "start_raw": s["start"], "end_raw": s["end"]}
                    for s in available_slots
                ],
                "total_slots": len(all_slots),
                "available_count": len(available_slots),
            }

        elif tool_name == "book_appointment":
            date_str = tool_args.get("date")
            start_time_str = tool_args.get("start_time")
            phone = tool_args.get("phone")

            if date_str and start_time_str and phone:
                existing = await sync_to_async(
                    lambda: Appointment.objects.filter(
                        date=date_str, start_time=start_time_str, phone=phone
                    ).first()
                )()
                if existing:
                    return AppointmentSerializer(existing).data

            serializer = AppointmentSerializer(data=tool_args)
            is_valid = await sync_to_async(serializer.is_valid)()
            if not is_valid:
                return {"error": True, "details": serializer.errors}

            appointment_date = serializer.validated_data.get("date")
            start_time = serializer.validated_data.get("start_time")
            end_time = serializer.validated_data.get("end_time")
            now_pk = datetime.now(pk_tz)

            if appointment_date < date_cls.today():
                return {"error": True, "message": "Appointment date cannot be in the past."}

            if appointment_date == now_pk.date() and start_time <= now_pk.time():
                return {
                    "error": True,
                    "message": f"Cannot book {start_time.strftime('%H:%M')} today — it is already {now_pk.strftime('%H:%M')}.",
                }

            overlap = await sync_to_async(
                lambda: Appointment.objects.filter(
                    date=appointment_date,
                    status__in=["pending", "confirmed"],
                    start_time__lt=end_time,
                    end_time__gt=start_time,
                ).exists()
            )()
            if overlap:
                return {"error": True, "message": "Time slot not available — conflicts with an existing appointment."}

            appointment = await sync_to_async(serializer.save)()

            def _background_tasks(appt_id):
                try:
                    from appointment.models import Appointment as Appt
                    import requests
                    from appointment.serializers import AppointmentSerializer

                    appt = Appt.objects.get(id=appt_id)
                    try:
                        app_url = os.environ.get("NEXT_PUBLIC_APP_URL", "http://localhost:3000")
                        if not app_url.startswith(("http://", "https://")):
                            app_url = "http://" + app_url
                        url = app_url.rstrip("/") + "/api/email"
                        data = AppointmentSerializer(appt).data
                        requests.post(url, json=data, timeout=10)
                        logger.info(f"Email triggered for appointment {appt_id}")
                    except Exception as ee:
                        logger.error(f"Email error: {ee}")
                except Exception as e:
                    logger.error(f"Background task error: {e}")

            threading.Thread(target=_background_tasks, args=(appointment.id,), daemon=True).start()
            return AppointmentSerializer(appointment).data

        else:
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        logger.error(f"Tool execution error [{tool_name}]: {e}")
        return {"error": str(e)}
