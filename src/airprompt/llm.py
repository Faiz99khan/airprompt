"""Claude Opus via Agent SDK with streaming, sentence splitting, history persistence.

Authentication piggybacks on the `claude` CLI's OAuth (Pro subscription). Run
`claude login` once before using.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from .attachments import Attachment, render_attachments_section
from .personality import Personality, render as render_personality

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"
SESSIONS_DIR = Path.home() / ".local/share/airprompt/sessions"


@dataclass
class Turn:
    role: str          # "user" or "assistant"
    content: str
    ts: float = field(default_factory=time.time)


def emit_sentences(buf: str, min_len: int = 40, force_flush: bool = False) -> tuple[list[str], str]:
    """Pull complete sentences out of a running buffer.

    Returns (sentences, remainder). A sentence ends at `.!?` followed by whitespace
    or end-of-buffer; we require min_len chars to avoid fragmenting on abbreviations
    like "Mr." or "e.g.". With force_flush, any remaining trimmed text is emitted.
    """
    out: list[str] = []
    cursor = 0
    i = 0
    n = len(buf)
    while i < n:
        c = buf[i]
        if c in ".!?":
            nxt = buf[i + 1] if i + 1 < n else ""
            if nxt == "" or nxt.isspace():
                candidate = buf[cursor : i + 1].strip()
                if len(candidate) >= min_len:
                    out.append(candidate)
                    cursor = i + 1
                    while cursor < n and buf[cursor].isspace():
                        cursor += 1
                    i = cursor
                    continue
        i += 1
    if force_flush and cursor < n:
        tail = buf[cursor:].strip()
        if tail:
            out.append(tail)
            cursor = n
    return out, buf[cursor:]


class InterviewerLLM:
    """Streaming Claude Opus client with persistent interview history."""

    def __init__(
        self,
        personality: Personality,
        role: str,
        attachments: list[Attachment] | None = None,
        session_path: Path | None = None,
        continue_path: Path | None = None,
    ) -> None:
        self.personality = personality
        self.role = role
        self.attachments = attachments or []
        self.history: list[Turn] = []
        self._system_prompt = render_personality(
            personality, role, render_attachments_section(self.attachments)
        )
        # Resuming: load prior turns NOW (so __aenter__ can replay them),
        # and keep writing to the same file so the conversation stays in one place.
        if continue_path is not None and continue_path.exists():
            self.load_history(continue_path)
            self.session_path = continue_path
        else:
            self.session_path = session_path or new_session_path()
        self._client = None
        self._client_ctx = None
        # Set by interrupt() on barge-in. While true, _stream_query_sentences
        # stops yielding but keeps consuming the SDK stream to its
        # ResultMessage, so the next turn doesn't inherit stale messages.
        self._interrupted = False

    async def __aenter__(self) -> "InterviewerLLM":
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        effort = self.personality.defaults.get("effort", "xhigh")
        model = self.personality.defaults.get("model", MODEL)
        options = ClaudeAgentOptions(
            model=model,
            effort=effort,
            system_prompt=self._system_prompt,
            include_partial_messages=True,
        )
        self._client_ctx = ClaudeSDKClient(options=options)
        self._client = await self._client_ctx.__aenter__()
        log.info(
            "Claude Agent SDK session started (model=%s, personality=%s, role=%s, attachments=%d)",
            model, self.personality.name, self.role, len(self.attachments),
        )
        log.debug("system prompt:\n%s", self._system_prompt)
        if self.history:
            await self._replay_history()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client_ctx is not None:
            await self._client_ctx.__aexit__(exc_type, exc, tb)
            self._client_ctx = None
            self._client = None

    async def _replay_history(self) -> None:
        """Tell Claude about prior turns so it can resume context."""
        transcript_lines = []
        for turn in self.history:
            speaker = "Candidate" if turn.role == "user" else "Interviewer"
            transcript_lines.append(f"{speaker}: {turn.content}")
        recap = (
            "We are resuming an in-progress interview. Here is the prior transcript:\n\n"
            + "\n".join(transcript_lines)
            + "\n\nContinue the interview from here with your next question. "
            "Acknowledge briefly that we're picking back up, then ask."
        )
        log.info("replaying %d prior turns", len(self.history))
        await self._client.query(recap)
        async for _ in self._client.receive_response():
            pass  # consume; we don't speak the recap response yet

    async def _stream_query_sentences(self, query_text: str) -> AsyncIterator[str]:
        """Send `query_text` to Claude and yield assistant sentences as they arrive.

        After the iterator completes (or is cancelled), `self._last_full_text`
        holds the full accumulated reply — callers that need it (history save,
        feedback markdown write) read it from there.
        """
        from claude_agent_sdk import AssistantMessage, TextBlock

        self._interrupted = False
        await self._client.query(query_text)

        buf = ""
        full_text = ""
        seen_text = ""
        yielded_any = False
        self._last_full_text = ""

        try:
            async for msg in self._client.receive_response():
                # On barge-in we stop processing/yielding but keep consuming
                # the stream to its ResultMessage. A half-read stream leaves
                # this turn's tail buffered in the SDK, and the next turn's
                # receive_response() would read it instead of its own reply —
                # shifting every later response one turn behind.
                if self._interrupted:
                    continue
                new_text = ""
                if isinstance(msg, AssistantMessage):
                    pieces = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            pieces.append(block.text)
                    msg_text = "".join(pieces)
                    if msg_text.startswith(seen_text):
                        new_text = msg_text[len(seen_text):]
                        seen_text = msg_text
                    else:
                        new_text = msg_text
                        seen_text = msg_text
                else:
                    delta = _extract_text_delta(msg)
                    if delta:
                        new_text = delta
                        seen_text += delta

                if not new_text:
                    continue
                buf += new_text
                full_text += new_text
                # First sentence uses a low threshold so short openers
                # ("Sure.", "Got it.", "Makes sense.") emit immediately
                # instead of being glued to the next sentence. 5 still skips
                # common abbreviations (Mr., U.S., i.e., e.g., Inc.).
                min_len = 5 if not yielded_any else 40
                sentences, buf = emit_sentences(buf, min_len=min_len, force_flush=False)
                for s in sentences:
                    if self._interrupted:
                        break
                    yielded_any = True
                    yield s

            if not self._interrupted:
                sentences, buf = emit_sentences(buf, min_len=1, force_flush=True)
                for s in sentences:
                    yield s
        finally:
            self._last_full_text = (full_text + buf).strip()

    async def stream_reply(self, user_text: str) -> AsyncIterator[str]:
        """Send `user_text` and yield assistant sentences as they become ready.

        On cancellation (barge-in), the partial response accumulated so far is
        still appended to history so context stays coherent.
        """
        self.history.append(Turn(role="user", content=user_text))
        self._save()
        try:
            async for s in self._stream_query_sentences(user_text):
                yield s
        finally:
            tail = self._last_full_text
            if tail:
                self.history.append(Turn(role="assistant", content=tail))
                self._save()

    async def stream_feedback(self) -> AsyncIterator[str]:
        """Yield sentences of a personality-driven feedback report.

        Does NOT touch session.json — the feedback is its own artifact
        (written to a Markdown file by the caller). After iteration completes,
        the full Markdown is available as `self._last_full_text`.
        """
        from . import feedback as feedback_mod

        turns = [
            {"role": t.role, "content": t.content, "ts": t.ts}
            for t in self.history
        ]
        attachments_section = render_attachments_section(self.attachments)
        user_prompt = feedback_mod.render_feedback_prompt(
            self.personality,
            role=self.role,
            attachments_section=attachments_section,
            turns=turns,
        )
        async for s in self._stream_query_sentences(user_prompt):
            yield s

    async def interrupt(self) -> None:
        """Abort the current Claude generation (used for barge-in).

        Sets `_interrupted` so the in-flight `_stream_query_sentences` stops
        yielding sentences but keeps draining the SDK stream to its
        ResultMessage. The drain is what keeps turns aligned — see the note
        in `_stream_query_sentences`.
        """
        self._interrupted = True
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception:  # noqa: BLE001
            log.exception("client.interrupt() failed")

    def load_history(self, path: Path) -> None:
        if not path.exists():
            return
        data = json.loads(path.read_text())
        # Backwards-compat: old sessions are a flat list of turns.
        turns = data["turns"] if isinstance(data, dict) else data
        self.history = [Turn(**d) for d in turns]
        log.info("loaded %d turns from %s", len(self.history), path)

    def _save(self) -> None:
        if self.session_path is None:
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "personality": self.personality.name,
            "role": self.role,
            "attachments": [
                {"label": a.label, "path": str(a.path)} for a in self.attachments
            ],
            "turns": [t.__dict__ for t in self.history],
        }
        self.session_path.write_text(json.dumps(payload, indent=2))


def _extract_text_delta(msg: object) -> str | None:
    """Best-effort extraction of an incremental text delta from a stream event."""
    # Common shapes seen across SDK versions
    for attr_path in ("delta.text", "event.delta.text", "content_block_delta.text"):
        cur = msg
        ok = True
        for part in attr_path.split("."):
            cur = getattr(cur, part, None)
            if cur is None:
                ok = False
                break
        if ok and isinstance(cur, str):
            return cur
    return None


def new_session_path() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR / f"session-{time.strftime('%Y%m%d-%H%M%S')}.json"
