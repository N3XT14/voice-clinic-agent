"""
main.py — FastAPI app tying the pipeline together.

Two entry points:
  POST /doctor/{appointment_id}/reschedule
      Simulates the doctor hitting "reschedule" in their own app. Kicks off
      the ping-pong logic in scheduler.py and returns the slot that will be
      offered to the patient. In a real system, this is also the trigger
      that would place an OUTBOUND call to the patient.

  WS   /ws/call/{doctor_id}
      Simulates an INBOUND patient call. Browser sends recorded audio
      blobs (push-to-talk), server replies with synthesized audio.
      This is the "voice AI orchestration" piece: ASR -> LLM(+tools) -> TTS.

"""
import base64
import tempfile
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import asr, tts
from .llm_agent import Agent
from .scheduler import Scheduler

app = FastAPI(title="Voice Clinic Agent")
scheduler = Scheduler()

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (FRONTEND_DIR / "index.html").read_text()


@app.post("/doctor/{doctor_id}/seed")
def seed_doctor(doctor_id: str):
    """Convenience endpoint for local testing — creates a demo doctor"""
    doc = scheduler.add_doctor("Dr. Rao", doctor_id=doctor_id, work_start="09:00", work_end="17:00", slot_minutes=20)
    return {"doctor_id": doc.id, "name": doc.name}


@app.post("/doctor/{appointment_id}/reschedule")
def doctor_reschedule(appointment_id: str):
    appt = scheduler.doctor_reschedule(appointment_id)
    return {
        "appointment_id": appt.id,
        "status": appt.status,
        "offered_date": appt.date,
        "offered_time": appt.time,
        "history": appt.history,
    }


@app.websocket("/ws/call/{doctor_id}")
async def call_session(websocket: WebSocket, doctor_id: str):
    """One WebSocket connection = one phone call.

    Protocol (kept deliberately simple for learning purposes):
      Client -> Server: raw audio bytes (webm/opus from MediaRecorder) per utterance
      Server -> Client: raw audio bytes (mp3 from edge-tts) as the reply

    A real telephony integration (Twilio Media Streams, etc.) would swap
    this transport but the ASR -> Agent -> TTS core stays identical —
    hence keeping this pipeline decoupled from transport.
    """
    await websocket.accept()
    agent = Agent(scheduler)

    greeting = "Hello, thank you for calling. How can I help you with your appointment today?"
    await _speak(websocket, greeting)

    try:
        while True:
            audio_bytes = await websocket.receive_bytes()

            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
                f.write(audio_bytes)
                in_path = f.name

            user_text = ""
            try:
                user_text = asr.transcribe(in_path)
            except Exception as e:
                print(f"[asr error] {e}")
                await _speak(websocket, "Sorry, I didn't catch that — could you say it again?")
                continue

            if not user_text:
                await _speak(websocket, "Sorry, I didn't catch that — could you say it again?")
                continue

            reply_text = agent.handle_turn(f"[doctor_id={doctor_id}] {user_text}")
            await _speak(websocket, reply_text)

    except WebSocketDisconnect:
        pass


async def _speak(websocket: WebSocket, text: str):
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        out_path = f.name
    await tts.synthesize_to_file(text, out_path)
    audio_bytes = Path(out_path).read_bytes()
    await websocket.send_json({"type": "transcript", "text": text})
    await websocket.send_bytes(audio_bytes)