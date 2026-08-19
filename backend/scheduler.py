"""
scheduler.py
============
The core domain logic for the clinic appointment negotiation.

--------------------------------------------------------------------
THE "PING-PONG" PROBLEM, AND HOW IT'S MODELED
--------------------------------------------------------------------
1. Patient requests a date/time -> appointment created, status=PENDING_DOCTOR
2. Doctor accepts               -> status=CONFIRMED
3. Doctor reschedules           -> status=RESCHEDULING
      - We build a ranked queue of candidate slots in the doctor's calendar over the next N days (default 14), closest to the original slot first.
      - We pop the first candidate and mark it AWAITING_PATIENT_CONFIRMATION.
      - (In the voice layer, this is the moment the system "calls" the patient and offers the new slot.)
4. Patient accepts the candidate -> slot is booked, status=CONFIRMED
   Patient declines ->  pop the next candidate from the queue and offer that one instead (the "pong" back to the patient again)
   Queue exhausted  ->  status=NEEDS_HUMAN_FOLLOWUP (a human receptionist takes over — the system should never silently drop a patient)

Every appointment keeps a `history` log of every transition so you can see
exactly what happened and when — useful both for debugging and for showing
off in an interview about this project.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db.json"


class Status:
    PENDING_DOCTOR = "PENDING_DOCTOR"
    CONFIRMED = "CONFIRMED"
    RESCHEDULING = "RESCHEDULING"
    AWAITING_PATIENT_CONFIRMATION = "AWAITING_PATIENT_CONFIRMATION"
    NEEDS_HUMAN_FOLLOWUP = "NEEDS_HUMAN_FOLLOWUP"
    CANCELLED = "CANCELLED"


@dataclass
class Doctor:
    id: str
    name: str
    work_start: str = "09:00"   # 24h "HH:MM"
    work_end: str = "17:00"
    slot_minutes: int = 20
    off_days: list = field(default_factory=lambda: [6])  # Mon=0 ... Sun=6


@dataclass
class Appointment:
    id: str
    patient_name: str
    patient_phone: str
    doctor_id: str
    date: str            # "YYYY-MM-DD" - the currently booked/proposed date
    time: str            # "HH:MM"
    status: str = Status.PENDING_DOCTOR
    candidate_queue: list = field(default_factory=list)  # list of [date,time]
    history: list = field(default_factory=list)

    def log(self, msg: str):
        self.history.append(f"{datetime.now().isoformat(timespec='seconds')} | {msg}")


class Store:
    """False DB for local"""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"doctors": {}, "appointments": {}})

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def _write(self, data: dict):
        self.path.write_text(json.dumps(data, indent=2))

    def get_doctor(self, doctor_id: str) -> Optional[Doctor]:
        d = self._read()["doctors"].get(doctor_id)
        return Doctor(**d) if d else None

    def upsert_doctor(self, doctor: Doctor):
        data = self._read()
        data["doctors"][doctor.id] = asdict(doctor)
        self._write(data)

    def get_appointment(self, appt_id: str) -> Optional[Appointment]:
        a = self._read()["appointments"].get(appt_id)
        return Appointment(**a) if a else None

    def upsert_appointment(self, appt: Appointment):
        data = self._read()
        data["appointments"][appt.id] = asdict(appt)
        self._write(data)

    def appointments_for_doctor(self, doctor_id: str) -> list[Appointment]:
        data = self._read()["appointments"]
        return [Appointment(**a) for a in data.values() if a["doctor_id"] == doctor_id]

    def find_appointments(self, patient_name: str = "", patient_phone: str = "") -> list[Appointment]:
        data = self._read()["appointments"]
        results = []
        for a in data.values():
            name_match = patient_name and patient_name.strip().lower() in a["patient_name"].lower()
            phone_match = patient_phone and patient_phone.strip() in a.get("patient_phone", "")
            if name_match or phone_match:
                results.append(Appointment(**a))
        return results


class Scheduler:
    def __init__(self, store: Optional[Store] = None):
        self.store = store or Store()

    # ---------- setup ----------
    def add_doctor(self, name: str, doctor_id: str = "", **kwargs) -> Doctor:
        doc = Doctor(id=doctor_id or str(uuid.uuid4())[:8], name=name, **kwargs)
        self.store.upsert_doctor(doc)
        return doc

    # ---------- slot generation ----------
    def _day_slots(self, doctor: Doctor, day: date) -> list[str]:
        if day.weekday() in doctor.off_days:
            return []
        start = datetime.combine(day, datetime.strptime(doctor.work_start, "%H:%M").time())
        end = datetime.combine(day, datetime.strptime(doctor.work_end, "%H:%M").time())
        slots = []
        cur = start
        while cur + timedelta(minutes=doctor.slot_minutes) <= end:
            slots.append(cur.strftime("%H:%M"))
            cur += timedelta(minutes=doctor.slot_minutes)
        return slots

    def available_slots(self, doctor_id: str, day: date, exclude_appt_id: str = "") -> list[str]:
        doctor = self.store.get_doctor(doctor_id)
        if not doctor:
            raise ValueError(f"Unknown doctor {doctor_id}")
        all_slots = self._day_slots(doctor, day)
        booked = {
            a.time
            for a in self.store.appointments_for_doctor(doctor_id)
            if a.date == day.isoformat()
            and a.status in (Status.CONFIRMED, Status.PENDING_DOCTOR)
            and a.id != exclude_appt_id
        }
        return [s for s in all_slots if s not in booked]

    # ---------- booking (initial patient request) ----------
    def request_appointment(self, patient_name: str, patient_phone: str,
                             doctor_id: str, preferred_date: str, preferred_time: str) -> Appointment:
        appt = Appointment(
            id=str(uuid.uuid4())[:8],
            patient_name=patient_name,
            patient_phone=patient_phone,
            doctor_id=doctor_id,
            date=preferred_date,
            time=preferred_time,
            status=Status.PENDING_DOCTOR,
        )
        appt.log(f"Patient requested {preferred_date} {preferred_time}")
        self.store.upsert_appointment(appt)
        return appt

    def doctor_accept(self, appt_id: str) -> Appointment:
        appt = self._require(appt_id)
        appt.status = Status.CONFIRMED
        appt.log("Doctor accepted")
        self.store.upsert_appointment(appt)
        return appt

    # ---------- the ping-pong reschedule flow ----------
    def doctor_reschedule(self, appt_id: str, horizon_days: int = 14) -> Appointment:
        """Doctor can no longer make the booked slot. Build a ranked
        candidate queue and offer the first one to the patient."""
        appt = self._require(appt_id)
        original_date = datetime.strptime(appt.date, "%Y-%m-%d").date()
        original_minutes = self._to_minutes(appt.time)

        candidates = []
        for offset in range(0, horizon_days + 1):
            day = original_date + timedelta(days=offset)
            for slot in self.available_slots(appt.doctor_id, day, exclude_appt_id=appt.id):
                if offset == 0 and slot == appt.time:
                    continue  # that's the slot doctor just cancelled
                score = offset * 1000 + abs(self._to_minutes(slot) - original_minutes)
                candidates.append((score, day.isoformat(), slot))
        candidates.sort(key=lambda c: c[0])

        appt.candidate_queue = [[d, t] for _, d, t in candidates]
        appt.status = Status.RESCHEDULING
        appt.log(f"Doctor rescheduled original slot; {len(candidates)} candidates found in next {horizon_days} days")
        self.store.upsert_appointment(appt)
        return self.offer_next_candidate(appt_id)

    def offer_next_candidate(self, appt_id: str) -> Appointment:
        appt = self._require(appt_id)
        if not appt.candidate_queue:
            appt.status = Status.NEEDS_HUMAN_FOLLOWUP
            appt.log("No more candidates in horizon — escalating to human receptionist")
            self.store.upsert_appointment(appt)
            return appt
        d, t = appt.candidate_queue.pop(0)
        appt.date, appt.time = d, t
        appt.status = Status.AWAITING_PATIENT_CONFIRMATION
        appt.log(f"Offering patient new slot {d} {t}")
        self.store.upsert_appointment(appt)
        return appt

    def patient_respond_to_offer(self, appt_id: str, accepted: bool) -> Appointment:
        appt = self._require(appt_id)
        if appt.status != Status.AWAITING_PATIENT_CONFIRMATION:
            raise ValueError("No pending offer for this appointment")
        if accepted:
            appt.status = Status.CONFIRMED
            appt.log(f"Patient accepted {appt.date} {appt.time}")
            self.store.upsert_appointment(appt)
            return appt
        appt.log(f"Patient declined {appt.date} {appt.time}")
        self.store.upsert_appointment(appt)
        return self.offer_next_candidate(appt_id)

    # ---------- helpers ----------
    def _require(self, appt_id: str) -> Appointment:
        appt = self.store.get_appointment(appt_id)
        if not appt:
            raise ValueError(f"Unknown appointment {appt_id}")
        return appt

    @staticmethod
    def _to_minutes(hhmm: str) -> int:
        h, m = map(int, hhmm.split(":"))
        return h * 60 + m