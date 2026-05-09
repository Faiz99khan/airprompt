"""Microphone capture and speaker playback via sounddevice.

The sounddevice callback runs in a PortAudio worker thread, so we bridge into
the asyncio loop with `loop.call_soon_threadsafe`.
"""
from __future__ import annotations

import asyncio
import logging
import queue
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

CAPTURE_SR = 16_000
CAPTURE_BLOCK = 512  # samples → 32 ms; matches Silero VAD chunk size
PLAYBACK_SR = 24_000  # Kokoro native rate


@dataclass
class AudioCapture:
    """Continuous mic capture. Pushes int16 mono frames to `out_queue`."""

    out_queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    device: int | None = None

    def __post_init__(self) -> None:
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.warning("input stream status: %s", status)
        # indata is float32 [-1,1]; convert to int16 mono 1-D
        mono = indata[:, 0] if indata.ndim > 1 else indata
        pcm16 = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
        try:
            self.loop.call_soon_threadsafe(self.out_queue.put_nowait, pcm16.copy())
        except RuntimeError:
            # loop closed during shutdown
            pass

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=CAPTURE_SR,
            blocksize=CAPTURE_BLOCK,
            channels=1,
            dtype="float32",
            callback=self._callback,
            device=self.device,
        )
        self._stream.start()
        log.info("mic capture started (sr=%d, block=%d)", CAPTURE_SR, CAPTURE_BLOCK)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class AudioPlayback:
    """Sequential playback. Feed float32 mono chunks at PLAYBACK_SR via `play`."""

    def __init__(self, device: int | None = None) -> None:
        self._device = device
        self._chunk_q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=64)
        self._stream: sd.OutputStream | None = None
        self._cur: np.ndarray | None = None
        self._cur_pos = 0

    def _callback(self, outdata, frames, time_info, status) -> None:
        if status:
            log.warning("output stream status: %s", status)
        out_idx = 0
        while out_idx < frames:
            if self._cur is None or self._cur_pos >= len(self._cur):
                try:
                    self._cur = self._chunk_q.get_nowait()
                except queue.Empty:
                    outdata[out_idx:, 0] = 0.0
                    return
                self._cur_pos = 0
                if self._cur is None:
                    # sentinel — drain silence
                    outdata[out_idx:, 0] = 0.0
                    return
            take = min(frames - out_idx, len(self._cur) - self._cur_pos)
            outdata[out_idx : out_idx + take, 0] = self._cur[self._cur_pos : self._cur_pos + take]
            self._cur_pos += take
            out_idx += take

    def start(self) -> None:
        self._stream = sd.OutputStream(
            samplerate=PLAYBACK_SR,
            blocksize=0,
            channels=1,
            dtype="float32",
            callback=self._callback,
            device=self._device,
        )
        self._stream.start()
        log.info("speaker playback started (sr=%d)", PLAYBACK_SR)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def play(self, audio: np.ndarray) -> None:
        """Enqueue a float32 mono audio chunk at PLAYBACK_SR."""
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        self._chunk_q.put(audio)

    def is_idle(self) -> bool:
        """True when no buffered audio remains and current chunk is fully consumed."""
        return self._chunk_q.empty() and (self._cur is None or self._cur_pos >= len(self._cur))

    async def wait_drained(self, poll_interval: float = 0.05) -> None:
        while not self.is_idle():
            await asyncio.sleep(poll_interval)

    def flush(self) -> None:
        """Drop all queued audio. Used for interruption/cleanup."""
        try:
            while True:
                self._chunk_q.get_nowait()
        except queue.Empty:
            pass
        self._cur = None
        self._cur_pos = 0
