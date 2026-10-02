"""Provider-neutral Telugu narration adapters for the Final Reel composer.

edge_tts is the default production narrator (free Microsoft Edge neural TTS, no API key).
sarvam is an optional paid adapter kept behind the same interface; Apple `say` remains the
final offline fallback. Changing voice or provider must never rebuild image assets.
"""

import asyncio
import json
import os
from pathlib import Path

EDGE_TTS_PROVIDER = "edge_tts"
EDGE_TTS_LANGUAGE = "te-IN"
EDGE_TTS_VOICE = "te-IN-MohanNeural"
EDGE_TTS_DEFAULT_VOICE = EDGE_TTS_VOICE
EDGE_TTS_FALLBACK_VOICE = "te-IN-ShrutiNeural"
EDGE_TTS_RATE = -12
EDGE_TTS_PREVIEW_RATES = (-8, -12, -16)
SARVAM_PROVIDER = "sarvam"
SARVAM_DEFAULT_VOICE = "shubh"
SARVAM_FALLBACK_VOICE = "ratan"


class NarrationProviderError(RuntimeError):
    pass


def _edge_tts_available():
    try:
        import edge_tts  # noqa: F401
        return True
    except ImportError:
        return False


def edge_rate(value):
    """Edge TTS rate string, e.g. -12 -> '-12%'."""
    return f"{int(value):+d}%"


def synthesize_edge(text, out_path, *, voice=EDGE_TTS_DEFAULT_VOICE, rate_percent=-12, language=EDGE_TTS_LANGUAGE):
    """Synthesize one Telugu line with edge_tts to an MP3 file path."""
    if not _edge_tts_available():
        raise NarrationProviderError("edge-tts is not installed.")
    import edge_tts as edge

    async def run():
        communicate = edge.Communicate(text=text, voice=voice, rate=edge_rate(rate_percent))
        await communicate.save(str(out_path))

    try:
        asyncio.run(run())
    except Exception as error:  # network/voice failure
        raise NarrationProviderError(f"edge_tts synthesis failed: {error}") from error
    if not Path(out_path).exists() or Path(out_path).stat().st_size < 200:
        raise NarrationProviderError("edge_tts produced no audio.")
    return str(out_path)


# Spoken-Telugu normalization applied to TTS input only; display text keeps digits.
def normalize_years_for_speech(text):
    """Replace year tokens with spoken Telugu words so TTS never reads them digit by digit."""
    replacements = {
        "2025–26": "రెండు వేల ఇరవై ఐదు - ఇరవై ఆరు",
        "2025-26": "రెండు వేల ఇరవై ఐదు - ఇరవై ఆరు",
        "2025": "రెండు వేల ఇరవై ఐదు",
        "2026": "రెండు వేల ఇరవై ఆరు",
    }
    normalized = str(text)
    for token in ("2025–26", "2025-26", "2025", "2026"):
        normalized = normalized.replace(token, replacements[token])
    return normalized


def synthesize_edge_continuous(text, out_path, *, voice=EDGE_TTS_VOICE, rate_percent=-11, language=EDGE_TTS_LANGUAGE):
    """Synthesize the WHOLE narration in one edge_tts request (no chunk joins)."""
    if not _edge_tts_available():
        raise NarrationProviderError("edge-tts is not installed.")
    import edge_tts as edge

    async def run():
        communicate = edge.Communicate(text=text, voice=voice, rate=edge_rate(rate_percent))
        await communicate.save(str(out_path))

    try:
        asyncio.run(run())
    except Exception as error:
        raise NarrationProviderError(f"edge_tts synthesis failed: {error}") from error
    if not Path(out_path).exists() or Path(out_path).stat().st_size < 200:
        raise NarrationProviderError("edge_tts produced no audio.")
    return str(out_path)


def narration_provider_status():
    return {
        "default_provider": EDGE_TTS_PROVIDER,
        "default_voice": EDGE_TTS_DEFAULT_VOICE,
        "fallback_voice": EDGE_TTS_FALLBACK_VOICE,
        "edge_tts_installed": _edge_tts_available(),
        "sarvam_configured": bool(os.environ.get("SARVAM_API_KEY")),
        "apple_offline_fallback": "Geeta (te_IN)",
        "language": EDGE_TTS_LANGUAGE,
    }
