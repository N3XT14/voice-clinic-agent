"""
asr.py — Speech-to-text using faster-whisper
"""
from faster_whisper import WhisperModel

_model: WhisperModel | None = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        # compute_type="int8" keeps this usable on CPU-only machines
        _model = WhisperModel("base", device="cpu", compute_type="int8")
    return _model


def transcribe(audio_path: str) -> str:
    """Transcribe a WAV/webm/etc file on disk and return plain text."""
    model = get_model()
    segments, _info = model.transcribe(audio_path, language="en", vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()
