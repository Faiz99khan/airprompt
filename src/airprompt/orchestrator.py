"""Asyncio orchestrator with true barge-in.

Topology:
    mic_callback (thread) ─► raw_audio_q ─► vad_task ─► utterance_q ─► stt_task
                                                                          │
    playback ◄── audio chunks ◄── _run_turn (LLM stream → Kokoro) ◄───────┘

State machine:
    LISTENING → THINKING (VAD endpoint with non-empty transcript)
    THINKING  → SPEAKING (first TTS audio enqueued)
    SPEAKING  → LISTENING (playback drained)
    {THINKING, SPEAKING} → LISTENING (user starts talking — barge-in)

Barge-in: VAD runs continuously regardless of state. While the assistant is
speaking, a higher VAD threshold is used (see vad.py) to avoid false-positive
interrupts from speaker bleed-through. On a confirmed speech-start while not
LISTENING, the current turn is cancelled, the playback queue is flushed, and
the in-flight Claude stream is aborted via the SDK's `interrupt()`.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from pathlib import Path

import numpy as np

from .attachments import load_attachment
from .audio_io import AudioCapture, AudioPlayback
from .llm import InterviewerLLM
from .personality import bootstrap_user_dir, load as load_personality
from .stt import Transcriber
from .tts import Synthesizer
from .vad import Endpointer

log = logging.getLogger(__name__)


class State(enum.Enum):
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Orchestrator:
    def __init__(
        self,
        personality_name: str,
        role: str,
        attach_specs: list[str] | None = None,
        continue_path: Path | None = None,
        input_device: int | None = None,
        output_device: int | None = None,
    ) -> None:
        bootstrap_user_dir()
        self.personality = load_personality(personality_name)
        self.role = role
        self.attachments = [load_attachment(s) for s in (attach_specs or [])]
        if self.attachments and not self.personality.uses_attachments:
            log.warning(
                "personality %r doesn't declare uses_attachments=true; "
                "attachments will only appear if its template references {attachments_section}",
                self.personality.name,
            )
        self.continue_path = continue_path
        self.input_device = input_device
        self.output_device = output_device

        self.state = State.LISTENING
        self._state_lock = asyncio.Lock()

        self.raw_audio_q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=256)
        self.utterance_q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=4)

        self._turn_task: asyncio.Task | None = None
        self._llm: InterviewerLLM | None = None
        self._synth: Synthesizer | None = None
        self._playback: AudioPlayback | None = None

    async def _set_state(self, new: State) -> None:
        async with self._state_lock:
            if self.state != new:
                log.info("state: %s → %s", self.state.value, new.value)
                self.state = new

    def is_speaking(self) -> bool:
        return self.state == State.SPEAKING

    async def vad_task(self, ep: Endpointer) -> None:
        while True:
            frame = await self.raw_audio_q.get()
            event = ep.process(frame)
            if event is None:
                continue
            kind = event[0]
            if kind == "start":
                if self.state != State.LISTENING:
                    asyncio.create_task(self._barge_in())
            elif kind == "end":
                utterance = event[1]
                if self.state == State.LISTENING:
                    await self._set_state(State.THINKING)
                    try:
                        self.utterance_q.put_nowait(utterance)
                    except asyncio.QueueFull:
                        log.warning("utterance queue full — dropping")
                        await self._set_state(State.LISTENING)
                else:
                    log.debug("ignoring end event in state %s", self.state.value)

    async def stt_task(self, transcriber: Transcriber) -> None:
        while True:
            audio = await self.utterance_q.get()
            t0 = time.perf_counter()
            text = await asyncio.to_thread(transcriber.transcribe, audio)
            log.info("[turn] STT %d ms: %r", int((time.perf_counter() - t0) * 1000), text)
            if not text or len(text.strip()) < 2:
                await self._set_state(State.LISTENING)
                continue
            print(f"\n\033[36m[you]\033[0m {text}")

            # cancel any prior turn (rare — would mean STT fired during a previous turn)
            if self._turn_task and not self._turn_task.done():
                self._turn_task.cancel()
                try:
                    await self._turn_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

            self._turn_task = asyncio.create_task(self._run_turn(text))

    async def _run_turn(self, user_text: str) -> None:
        assert self._llm and self._synth and self._playback
        t0 = time.perf_counter()
        first = True
        print("\033[35m[claude]\033[0m ", end="", flush=True)
        try:
            async for sentence in self._llm.stream_reply(user_text):
                if first:
                    log.info("[turn] LLM first sentence %d ms", int((time.perf_counter() - t0) * 1000))
                    first = False
                print(sentence + " ", end="", flush=True)
                audio = await asyncio.to_thread(self._synth.synth, sentence)
                if audio.size == 0:
                    continue
                if self.state != State.SPEAKING:
                    await self._set_state(State.SPEAKING)
                self._playback.play(audio)
            print()
            await self._playback.wait_drained()
            await self._set_state(State.LISTENING)
        except asyncio.CancelledError:
            print()
            log.info("turn cancelled (barge-in)")
            raise

    async def _barge_in(self) -> None:
        if self._turn_task is None or self._turn_task.done():
            # state is THINKING/SPEAKING but no turn task — nothing to cancel; just reset.
            await self._set_state(State.LISTENING)
            return
        log.info("barge-in detected — cancelling turn")
        if self._llm is not None:
            try:
                await self._llm.interrupt()
            except Exception:  # noqa: BLE001
                log.exception("llm.interrupt() failed")
        if self._playback is not None:
            self._playback.flush()
        self._turn_task.cancel()
        try:
            await self._turn_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await self._set_state(State.LISTENING)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        log.info("loading models…")
        ep = await asyncio.to_thread(Endpointer, self.is_speaking)
        transcriber = await asyncio.to_thread(Transcriber)
        self._synth = await asyncio.to_thread(Synthesizer)

        capture = AudioCapture(out_queue=self.raw_audio_q, loop=loop, device=self.input_device)
        self._playback = AudioPlayback(device=self.output_device)
        # Playback can start now — it doesn't accumulate; we only feed it when speaking.
        # Mic is deferred until the VAD task is running and history has been replayed,
        # otherwise startup latency (model load + Claude history replay on --continue)
        # overflows the input queue.
        self._playback.start()

        async with InterviewerLLM(
            personality=self.personality,
            role=self.role,
            attachments=self.attachments,
            continue_path=self.continue_path,
        ) as llm:
            self._llm = llm
            log.info("session file: %s", llm.session_path)

            tasks = [
                asyncio.create_task(self.vad_task(ep), name="vad"),
                asyncio.create_task(self.stt_task(transcriber), name="stt"),
            ]

            # Now that consumers are alive, open the mic.
            capture.start()

            attach_note = (
                f" + {len(self.attachments)} attachment(s)" if self.attachments else ""
            )
            print(
                f"\n\033[32m[ready]\033[0m {self.personality.name} for: {self.role}{attach_note}. "
                "Speak when ready. Interrupt anytime. Ctrl+C to end.\n"
            )
            try:
                await asyncio.gather(*tasks)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                if self._turn_task and not self._turn_task.done():
                    self._turn_task.cancel()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                capture.stop()
                self._playback.stop()
