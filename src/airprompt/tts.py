"""Text-to-speech via Kokoro-82M (open source, fast, natural)."""
from __future__ import annotations

import logging
import sys
import time

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_VOICE = "af_heart"   # American English, pleasant default
DEFAULT_LANG = "a"           # 'a' = American English in Kokoro


class Synthesizer:
    def __init__(self, lang_code: str = DEFAULT_LANG, voice: str = DEFAULT_VOICE) -> None:
        from kokoro import KPipeline

        log.info("loading Kokoro pipeline (lang=%s, voice=%s)…", lang_code, voice)
        t0 = time.perf_counter()
        self._pipeline = KPipeline(lang_code=lang_code)
        self._voice = voice
        log.info("Kokoro loaded in %.2fs", time.perf_counter() - t0)

    def synth(self, text: str) -> np.ndarray:
        """Synthesize a sentence. Returns float32 mono at 24 kHz."""
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        t0 = time.perf_counter()
        chunks: list[np.ndarray] = []
        for _gs, _ps, audio in self._pipeline(text, voice=self._voice, speed=1.0):
            arr = audio.cpu().numpy() if hasattr(audio, "cpu") else np.asarray(audio)
            chunks.append(arr.astype(np.float32))
        out = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        dur_ms = int(len(out) / 24_000 * 1000)
        log.info("TTS %d ms compute → %d ms audio (%r)", int((time.perf_counter() - t0) * 1000), dur_ms, text[:60])
        return out


def _smoke_test(text: str) -> None:
    import sounddevice as sd

    s = Synthesizer()
    audio = s.synth(text)
    sd.play(audio, samplerate=24_000)
    sd.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(sys.argv) < 2:
        raise SystemExit('usage: python -m airprompt.tts "Hello world"')
    _smoke_test(" ".join(sys.argv[1:]))
