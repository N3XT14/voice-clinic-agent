"""
tts.py — Text-to-speech using edge-tts .
"""
import edge_tts

VOICE = "en-IN-NeerjaNeural"  # Indian English, female. en-IN-PrabhatNeural for male.


async def synthesize_to_file(text: str, out_path: str, voice: str = VOICE):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)
    return out_path
