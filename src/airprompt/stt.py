"""Speech-to-text via faster-whisper large-v3 on GPU."""
from __future__ import annotations

import logging
import sys
import time

import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "large-v3"
SAMPLE_RATE = 16_000

# Whisper's decoder normalizes disfluencies away — "um"/"uh" and stutters
# rarely survive into the text. Seeding the prompt with fillers biases it
# toward verbatim output, which the coaching personalities depend on.
# Pass initial_prompt=None to get Whisper's default cleaned-up transcription.
VERBATIM_PROMPT = "Umm, let me think like, hmm... Okay, here's what I'm, like, thinking."


class Transcriber:
    def __init__(
        self,
        model_name: str = MODEL_NAME,
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "en",
        initial_prompt: str | None = VERBATIM_PROMPT,
    ) -> None:
        from faster_whisper import WhisperModel

        log.info("loading faster-whisper %s on %s (%s)…", model_name, device, compute_type)
        t0 = time.perf_counter()
        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)
        log.info("faster-whisper loaded in %.2fs", time.perf_counter() - t0)
        self._language = language
        self._initial_prompt = initial_prompt

    def transcribe(self, audio_int16: np.ndarray) -> str:
        """Transcribe int16 mono audio at 16 kHz. Returns concatenated text."""
        if audio_int16.dtype != np.int16:
            raise ValueError("expected int16 audio")
        audio_f32 = audio_int16.astype(np.float32) / 32768.0
        t0 = time.perf_counter()
        segments, _info = self._model.transcribe(
            audio_f32,
            beam_size=1,
            language=self._language,
            initial_prompt=self._initial_prompt,
            vad_filter=False,           # already gated by Silero VAD
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        log.info("STT %d ms → %r", int((time.perf_counter() - t0) * 1000), text)
        return text


def _smoke_test(path: str) -> None:
    import soundfile as sf

    data, sr = sf.read(path, dtype="int16")
    if sr != SAMPLE_RATE:
        raise SystemExit(f"expected 16 kHz wav, got {sr}")
    if data.ndim > 1:
        data = data[:, 0]
    t = Transcriber()
    print(t.transcribe(data))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m airprompt.stt <wav_path>")
    _smoke_test(sys.argv[1])
