# Voice Clinic Agent

A learning project for real-time conversational voice AI orchestration
(ASR → LLM → TTS over a WebSocket pipeline), built around a genuinely useful
use case: an Indian clinic appointment assistant that handles the
patient ⇄ doctor scheduling "ping-pong."

## The problem this solves

1. Patient requests an appointment (date + time).
2. Doctor accepts — done.
3. Doctor reschedules — the system automatically searches the doctor's
   calendar over the **next 14 days**, finds the closest available slot to
   the original request, and offers it back to the patient.
4. Patient accepts → booked. Patient declines → the system offers the next
   closest slot. This repeats until either something is confirmed or the
   14-day window is exhausted (at which point it escalates to a human
   receptionist instead of silently failing).

That negotiation loop is implemented as an explicit state machine in
[`backend/scheduler.py`](backend/scheduler.py), **completely decoupled** from
the voice pipeline. That's a deliberate design choice — it's the part of
the system with real business logic, and you want to be able to unit test
it without a microphone. Run it standalone:

```bash
python tests/test_scheduler.py
```

## Architecture

```
Browser (mic)                     FastAPI backend
   |  push-to-talk audio blob         |
   |--------------------------------->|  /ws/call/{doctor_id}
   |                                  |
   |                             asr.py: faster-whisper (local, free)
   |                                  |  transcript
   |                                  v
   |                             llm_agent.py: Groq (or local Ollama)
   |                                  |  - holds conversation history
   |                                  |  - decides when to call tools:
   |                                  |      check_availability()
   |                                  |      book_appointment()
   |                                  |      respond_to_offer()
   |                                  |  - tools call straight into scheduler.py
   |                                  v
   |                             tts.py: edge-tts (local-ish, free)
   |<---------------------------------|  reply audio (mp3)
   |  plays audio                     |

Doctor side (simulated for now):
   POST /doctor/{appointment_id}/reschedule
        -> scheduler.doctor_reschedule()
        -> builds ranked candidate queue over next 14 days
        -> offers first candidate
        (in a real system, this is also the trigger for an OUTBOUND
         call to the patient to relay the new offer)
```

### Why this stack

| Layer      | Choice                     | Why |
|------------|----------------------------|-----|
| Transport  | Raw WebSocket, push-to-talk | Simplest possible thing that's still a real streaming pipeline. No telephony account needed to learn the core orchestration. |
| ASR        | `faster-whisper` (local)   | Free, no API key, runs on CPU. |
| LLM        | Groq (OpenAI-compatible API) | Free tier, very low latency — matters a lot for voice UX. Swappable to local Ollama with just an env var change. |
| TTS        | `edge-tts`                 | Free, no signup, has good Indian-English voices out of the box. |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then add a free Groq key from console.groq.com
uvicorn backend.main:app --reload
```

Open `http://localhost:8000`. Seed a demo doctor first:

```bash
curl -X POST http://localhost:8000/doctor/demo-doctor/seed
```

Hold the mic button and talk — try "I'd like an appointment next Thursday
at 10am." Then, in a second terminal, simulate the doctor rescheduling it:

```bash
curl -X POST http://localhost:8000/doctor/<appointment_id>/reschedule
```

Call back in and continue the conversation — the agent will relay the new
offered slot and you can accept/decline by voice.

## Where to take this next

Roughly in order of "how close to production this gets you":

1. **Real streaming instead of push-to-talk.** Add voice-activity detection
   (VAD) so the pipeline reacts to natural pauses instead of a button.
   `webrtcvad` or `silero-vad` are good starting points; you'd stream audio
   chunks continuously and run ASR incrementally.
2. **Real telephony.** Swap the browser WebSocket for Twilio Media Streams
   (or an Indian provider like Exotel/Knowlarity) — they also give you a
   WebSocket of raw audio, so `asr.py`/`llm_agent.py`/`tts.py` barely change;
   only `main.py`'s transport layer does. This is also how you'd implement
   the **outbound call to the patient** when a reschedule happens, instead
   of the patient having to call back in.
3. **Barge-in / interruption handling.** Let the patient interrupt the
   agent mid-sentence — requires cancelling in-flight TTS playback.
4. **Persistent storage.** Swap the JSON file in `scheduler.py`'s `Store`
   for Postgres/SQLite-with-SQLAlchemy once you're past prototyping.
5. **Multi-doctor / multi-clinic routing**, calendar sync (Google
   Calendar API), SMS confirmations, etc.

## Project layout

```
backend/
  scheduler.py   - the state machine (read this first)
  asr.py         - speech-to-text
  tts.py         - text-to-speech
  llm_agent.py   - conversation + tool-calling loop
  main.py        - FastAPI app, WebSocket wiring
frontend/
  index.html     - minimal push-to-talk demo UI
tests/
  test_scheduler.py - standalone scheduler walkthrough, no audio needed
```
