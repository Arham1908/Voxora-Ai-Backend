try:
    from google.genai import types
except ImportError:
    raise ImportError("pip install google-genai")

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="get_schedule",
                description=(
                    "Fetch the full weekly schedule of the practice. "
                    "Returns each day with is_active (bool), start_time, end_time, and slot_duration. "
                    "\n**Invocation Condition:** Invoke this tool immediately after the customer mentions an appointment or scheduling request. This must be the first tool called in any scheduling flow."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            types.FunctionDeclaration(
                name="get_available_slots",
                description=(
                    "Fetch available appointment slots for a specific date. "
                    "\n**Invocation Condition:** Invoke this tool only after validating the date is not in the past, is within 7 days from today, and is an open day (is_active: true) according to get_schedule. Must be called before book_appointment."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "date": types.Schema(
                            type=types.Type.STRING,
                            description="Date to check slots for, in YYYY-MM-DD format.",
                        ),
                    },
                    required=["date"],
                ),
            ),
            types.FunctionDeclaration(
                name="book_appointment",
                description=(
                    "Book an appointment after the patient has verbally confirmed all details. "
                    "\n**Invocation Condition:** Invoke this tool *only after* the patient has explicitly confirmed (said 'YES') to a specific date and time, and all personal details (name, phone, email) have been collected and verified."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "name":       types.Schema(type=types.Type.STRING, description="Full name of the patient."),
                        "phone":      types.Schema(type=types.Type.STRING, description="Phone number of the patient."),
                        "email":      types.Schema(type=types.Type.STRING, description="Valid email address (must contain @)."),
                        "date":       types.Schema(type=types.Type.STRING, description="Appointment date in YYYY-MM-DD format."),
                        "start_time": types.Schema(type=types.Type.STRING, description="Start time in HH:MM format."),
                        "end_time":   types.Schema(type=types.Type.STRING, description="End time in HH:MM format."),
                        "notes":      types.Schema(type=types.Type.STRING, description="Reason for the appointment."),
                    },
                    required=["name", "phone", "email", "date", "start_time", "end_time"],
                ),
            ),
        ]
    )
]
