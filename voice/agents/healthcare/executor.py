import logging
import threading
import zoneinfo
from datetime import datetime, timedelta, date as date_cls
from asgiref.sync import sync_to_async
from appointment.models import Schedule, Appointment
from appointment.serializers import ScheduleSerializer, AppointmentSerializer

logger = logging.getLogger(__name__)
pk_tz = zoneinfo.ZoneInfo("Asia/Karachi")


def _time_to_spoken(time_str: str) -> str:
    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        period = "AM" if hour < 12 else "PM"
        display_hour = hour % 12 or 12
        if minute == 0:
            return f"{display_hour} {period}"
        return f"{display_hour}:{minute:02d} {period}"
    except (ValueError, IndexError):
        return time_str


async def execute_tool(tool_name: str, tool_args: dict) -> dict:
    try:
        if tool_name == "get_schedule":
            schedules = await sync_to_async(
                lambda: list(Schedule.objects.all())
            )()
            data = ScheduleSerializer(schedules, many=True).data
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

            all_slots = []
            current = datetime.combine(date, schedule.start_time)
            end = datetime.combine(date, schedule.end_time)
            while current + timedelta(minutes=schedule.slot_duration) <= end:
                slot_end = current + timedelta(minutes=schedule.slot_duration)
                start_hhmm = current.strftime("%H:%M")
                end_hhmm = slot_end.strftime("%H:%M")
                all_slots.append({
                    "start": start_hhmm,
                    "end": end_hhmm,
                    "spoken_start": _time_to_spoken(start_hhmm),
                    "spoken_end": _time_to_spoken(end_hhmm),
                })
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
            return {
                "date": date_str,
                "day": day_display,
                "slot_duration": f"{schedule.slot_duration} mins",
                "total_slots": len(all_slots),
                "booked_slots": len(booked_times),
                "available_slots": len(available_slots),
                "slots": available_slots,
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
                    import os
                    from appointment.serializers import AppointmentSerializer

                    appt = Appt.objects.get(id=appt_id)

                    try:
                        url = os.environ.get("NEXT_PUBLIC_APP_URL", "http://localhost:3000") + "/api/email"
                        data = AppointmentSerializer(appt).data
                        requests.post(url, json=data, timeout=10)
                        logger.info(f"Triggered Next.js email API for appointment {appt_id}")
                    except Exception as ee:
                        logger.error(f"Background Email error: {ee}")

                except Exception as e:
                    logger.error(f"Background Task Management error: {e}")

            threading.Thread(target=_background_tasks, args=(appointment.id,), daemon=True).start()
            return AppointmentSerializer(appointment).data

        else:
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        logger.error(f"Tool execution error [{tool_name}]: {e}")
        return {"error": str(e)}
