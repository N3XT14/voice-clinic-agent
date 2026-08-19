"""
llm_agent.py — the "brain" of the call.

The model is given "tools" (function calling) that map directly onto the Scheduler methods in scheduler.py. 
Pattern:
    agents: ASR gives you text -> LLM decides *what to do* and calls a tool -> server execute the tool in real code -> feed the result back -> LLM produces the natural-language reply -> that reply goes to TTS.
"""
import json
import os
from datetime import date

from dotenv import load_dotenv
from openai import OpenAI

from .scheduler import Scheduler, Status

load_dotenv()

BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

SYSTEM_PROMPT = """You are a scheduling assistant for an Indian clinic, speaking to a patient
over the phone. Be warm, brief, and clear — this is a voice conversation, not chat, so keep
replies to 1-3 short sentences. Always confirm dates and times back to the patient in plain
words (e.g. "Thursday the 21st at 10 AM") since they cannot see a screen.

Today's date is {today}.

You can request an appointment, and you can respond on the patient's behalf when a rescheduled
slot is offered to them (ask them to accept or decline, then call respond_to_offer with what
they said). Never invent appointment IDs or slot times yourself — always get them from tool
results.

If the patient mentions an existing appointment (checking on it, responding to a reschedule
offer, cancelling, etc.), call find_appointment with their name first — do not ask them for an
appointment ID, they will not have one. Only call book_appointment if find_appointment comes
back with nothing and they are clearly asking for a brand-new booking. If find_appointment finds
a match with status AWAITING_PATIENT_CONFIRMATION, tell the patient the offered date/time and
ask if they accept it, then call respond_to_offer.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "List open slot times for a doctor on a given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_id": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["doctor_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_appointment",
            "description": "Look up an existing appointment by the patient's name and/or phone number. ALWAYS try this first if the patient mentions an appointment they already have (e.g. asking about a reschedule, confirming, or cancelling) — never assume no appointment exists just because you don't have its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string"},
                    "patient_phone": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Create a new appointment request for a patient. Status starts PENDING_DOCTOR until the doctor confirms.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string"},
                    "patient_phone": {"type": "string"},
                    "doctor_id": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM 24h"},
                },
                "required": ["patient_name", "patient_phone", "doctor_id", "date", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "respond_to_offer",
            "description": "Record the patient's accept/decline answer to a rescheduled slot that was offered to them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "accepted": {"type": "boolean"},
                },
                "required": ["appointment_id", "accepted"],
            },
        },
    },
]


class Agent:
    """One instance per call session. Holds the running message history so
    the model has conversational memory across turns of the same call."""

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(today=date.today().isoformat())}
        ]

    def _execute_tool(self, name: str, args: dict) -> dict:
        s = self.scheduler
        if name == "check_availability":
            slots = s.available_slots(args["doctor_id"], date.fromisoformat(args["date"]))
            return {"slots": slots}
        if name == "find_appointment":
            matches = s.find_appointments(
                patient_name=args.get("patient_name", ""),
                patient_phone=args.get("patient_phone", ""),
            )
            if not matches:
                return {"found": False}
            return {
                "found": True,
                "appointments": [
                    {"appointment_id": a.id, "status": a.status, "date": a.date, "time": a.time}
                    for a in matches
                ],
            }
        if name == "book_appointment":
            appt = s.request_appointment(
                args["patient_name"], args["patient_phone"],
                args["doctor_id"], args["date"], args["time"],
            )
            return {"appointment_id": appt.id, "status": appt.status}
        if name == "respond_to_offer":
            appt = s.patient_respond_to_offer(args["appointment_id"], args["accepted"])
            return {
                "status": appt.status,
                "offered_date": appt.date,
                "offered_time": appt.time,
            }
        return {"error": f"unknown tool {name}"}

    def handle_turn(self, user_text: str) -> str:
        """Feed one transcribed user utterance in, get the assistant's
        natural-language reply out (ready for TTS). Runs the full
        tool-calling loop internally."""
        self.messages.append({"role": "user", "content": user_text})

        for _ in range(4):  # cap tool-call round trips per turn
            resp = client.chat.completions.create(
                model=MODEL, messages=self.messages, tools=TOOLS,
            )
            msg = resp.choices[0].message
            self.messages.append(msg.model_dump(exclude_none=True))

            if not msg.tool_calls:
                return msg.content or ""

            for call in msg.tool_calls:
                args = json.loads(call.function.arguments or "{}")
                result = self._execute_tool(call.function.name, args)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result),
                })

        return "Sorry, I'm having trouble with that — let me get a receptionist to help you."