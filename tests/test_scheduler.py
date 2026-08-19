import sys
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scheduler import Scheduler, Store, Status

TEST_DB = Path(__file__).resolve().parent / "test_db.json"
if TEST_DB.exists():
    TEST_DB.unlink()

sched = Scheduler(store=Store(path=TEST_DB))

doc = sched.add_doctor("Dr. Rao", work_start="09:00", work_end="13:00", slot_minutes=20, off_days=[6])
print(f"Created doctor: {doc.name} ({doc.id})")

today = date.today()
target_date = (today + timedelta(days=3)).isoformat()

appt = sched.request_appointment(
    patient_name="Asha Verma",
    patient_phone="+91-9000000000",
    doctor_id=doc.id,
    preferred_date=target_date,
    preferred_time="10:00",
)
print(f"\n1) Patient requested appointment {appt.id}: {appt.date} {appt.time} [{appt.status}]")

appt = sched.doctor_accept(appt.id)
print(f"2) Doctor accepted: [{appt.status}]")

appt = sched.doctor_reschedule(appt.id, horizon_days=14)
print(f"3) Doctor rescheduled -> system offers: {appt.date} {appt.time} [{appt.status}]")
print(f"   ({len(appt.candidate_queue)} more candidates queued if this is declined)")

appt = sched.patient_respond_to_offer(appt.id, accepted=False)
print(f"4) Patient declined -> next offer: {appt.date} {appt.time} [{appt.status}]")

appt = sched.patient_respond_to_offer(appt.id, accepted=True)
print(f"5) Patient accepted -> final: {appt.date} {appt.time} [{appt.status}]")

print("\n--- Full history log ---")
for line in appt.history:
    print(" ", line)

assert appt.status == Status.CONFIRMED
print("\nOK: scheduler ping-pong flow works end to end.")
