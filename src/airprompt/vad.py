"""Silero VAD endpointing.

Consumes 32 ms / 512-sample int16 chunks at 16 kHz. `process()` returns:
    ("start",)              when speech begins
    ("end", utterance)      when speech ends (utterance is int16 mono 16 kHz)
    None                    otherwise

Barge-in: when `in_speaking_state()` returns True, a higher start-threshold
and a longer confirmation window are used so TTS audio bleeding into the mic
doesn't trigger spurious interrupts. Tune via env vars (see below).

Environment overrides
---------------------
AIRPROMPT_SILENCE_MS              ms of silence to end an utterance       [800]
AIRPROMPT_VAD_START_THRESHOLD     prob threshold for speech start         [0.5]
AIRPROMPT_VAD_END_THRESHOLD       prob threshold for silence              [0.35]
AIRPROMPT_VAD_START_FRAMES        consecutive frames to confirm start     [3]
AIRPROMPT_BARGE_THRESHOLD         start threshold while assistant speaks  [0.7]
AIRPROMPT_BARGE_FRAMES            consecutive frames to confirm barge-in  [5]
AIRPROMPT_PREROLL_MS              audio captured before speech start      [300]
AIRPROMPT_MIN_UTTERANCE_MS        drop utterances shorter than this       [250]
"""
from __future__ import annotations

import logging
import os
from collections import deque
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

SR = 16_000
FRAME_SAMPLES = 512  # 32 ms @ 16 kHz (Silero requirement)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


SILENCE_MS = _env_int("AIRPROMPT_SILENCE_MS", 800)
START_THRESHOLD = _env_float("AIRPROMPT_VAD_START_THRESHOLD", 0.5)
END_THRESHOLD = _env_float("AIRPROMPT_VAD_END_THRESHOLD", 0.35)
START_FRAMES = _env_int("AIRPROMPT_VAD_START_FRAMES", 3)
BARGE_THRESHOLD = _env_float("AIRPROMPT_BARGE_THRESHOLD", 0.7)
BARGE_FRAMES = _env_int("AIRPROMPT_BARGE_FRAMES", 5)
PREROLL_MS = _env_int("AIRPROMPT_PREROLL_MS", 300)
MIN_UTTERANCE_MS = _env_int("AIRPROMPT_MIN_UTTERANCE_MS", 250)

SILENCE_FRAMES = max(1, (SILENCE_MS * SR) // (FRAME_SAMPLES * 1000))
PREROLL_FRAMES = max(1, (PREROLL_MS * SR) // (FRAME_SAMPLES * 1000))


class Endpointer:
    def __init__(self, in_speaking_state: Callable[[], bool] = lambda: False) -> None:
        from silero_vad import load_silero_vad

        self._model = load_silero_vad(onnx=True)
        self._in_speaking = in_speaking_state
        self._reset()
        log.info(
            "VAD config: silence=%d ms start_thr=%.2f end_thr=%.2f barge_thr=%.2f barge_frames=%d preroll=%d ms",
            SILENCE_MS,
            START_THRESHOLD,
            END_THRESHOLD,
            BARGE_THRESHOLD,
            BARGE_FRAMES,
            PREROLL_MS,
        )

    def _reset(self) -> None:
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._preroll: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        self._buffer: list[np.ndarray] = []
        try:
            self._model.reset_states()
        except Exception:  # noqa: BLE001
            pass

    def reset(self) -> None:
        self._reset()

    def process(self, frame_int16: np.ndarray):
        """Feed one 512-sample int16 frame. See module docstring for return shape."""
        import torch

        if frame_int16.shape[0] != FRAME_SAMPLES:
            log.warning("unexpected frame size %d (expected %d)", frame_int16.shape[0], FRAME_SAMPLES)
            return None

        frame_f32 = frame_int16.astype(np.float32) / 32768.0
        with torch.no_grad():
            prob = float(self._model(torch.from_numpy(frame_f32), SR).item())

        speaking = self._in_speaking()
        start_thr = BARGE_THRESHOLD if speaking else START_THRESHOLD
        start_frames = BARGE_FRAMES if speaking else START_FRAMES

        if not self._in_speech:
            self._preroll.append(frame_int16)
            if prob >= start_thr:
                self._speech_run += 1
                if self._speech_run >= start_frames:
                    self._in_speech = True
                    self._buffer = list(self._preroll)  # include pre-roll
                    self._preroll.clear()
                    self._silence_run = 0
                    log.debug("speech start (prob=%.2f, speaking=%s)", prob, speaking)
                    return ("start",)
            else:
                self._speech_run = 0
            return None

        # in speech
        self._buffer.append(frame_int16)
        if prob < END_THRESHOLD:
            self._silence_run += 1
            if self._silence_run >= SILENCE_FRAMES:
                utterance = np.concatenate(self._buffer)
                duration_ms = (len(utterance) * 1000) // SR
                self._reset()
                if duration_ms < MIN_UTTERANCE_MS:
                    log.debug("dropped short utterance (%d ms)", duration_ms)
                    return None
                log.info("utterance ended (%d ms)", duration_ms)
                return ("end", utterance)
        else:
            self._silence_run = 0
        return None
