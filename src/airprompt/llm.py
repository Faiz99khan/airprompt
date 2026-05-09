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

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"
SESSIONS_DIR = Path.home() / ".local/share/airprompt/sessions"


def interviewer_system_prompt(role: str) -> str:
    return (
        f"You are conducting a realistic mock job interview for the position of: {role}.\n\n"
        "How to conduct the interview:\n"
        "- Ask one focused question at a time, then wait for the candidate's reply.\n"
        "- Mix question types: behavioral, technical/role-specific, situational, and follow-ups that probe their answers.\n"
        "- Adapt difficulty and direction based on what the candidate says.\n"
        "- Reference earlier answers when useful.\n"
        "- After ~8–12 questions OR when the candidate says \"end interview\" / \"that's all\", give structured feedback: strengths, weaknesses, specific examples from their answers, and a hire/no-hire recommendation with reasoning.\n\n"
        "CRITICAL — be concise. This is a SPOKEN conversation, not a written essay:\n"
        "- Default reply length: 1–3 short sentences. Most replies should be under 30 words.\n"
        "- A typical question is one sentence. A brief acknowledgement (\"Got it.\" / \"Interesting.\") plus a follow-up question is the norm.\n"
        "- DO NOT narrate, summarize verbosely, or restate what the candidate just said back to them.\n"
        "- If the candidate asks \"what were we discussing?\" or similar, give a ONE-SENTENCE recap and immediately ask the next/pending question. Do not list multiple prior points.\n"
        "- The end-of-interview feedback is the ONLY time longer replies are appropriate.\n\n"
        "Voice formatting rules (spoken aloud by TTS):\n"
        "- Plain conversational prose only. NO markdown, bullets, headers, code blocks, asterisks, or numbered lists.\n"
        "- No emojis or non-speakable characters.\n"
        "- Spell out numbers naturally as a person would say them."
    )


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
        role: str,
        session_path: Path | None = None,
        resume_path: Path | None = None,
    ) -> None:
        self.role = role
        self.history: list[Turn] = []
        # Resuming: load prior turns NOW (so __aenter__ can replay them),
        # and keep writing to the same file so the conversation stays in one place.
        if resume_path is not None and resume_path.exists():
            self.load_history(resume_path)
            self.session_path = resume_path
        else:
            self.session_path = session_path or new_session_path()
        self._client = None
        self._client_ctx = None

    async def __aenter__(self) -> "InterviewerLLM":
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = ClaudeAgentOptions(
            model=MODEL,
            effort="xhigh",
            system_prompt=interviewer_system_prompt(self.role),
            include_partial_messages=True,
        )
        self._client_ctx = ClaudeSDKClient(options=options)
        self._client = await self._client_ctx.__aenter__()
        log.info("Claude Agent SDK session started (model=%s)", MODEL)
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

    async def stream_reply(self, user_text: str) -> AsyncIterator[str]:
        """Send `user_text` and yield assistant sentences as they become ready.

        On cancellation (barge-in), the partial response accumulated so far is
        still appended to history so context stays coherent.
        """
        from claude_agent_sdk import AssistantMessage, TextBlock

        self.history.append(Turn(role="user", content=user_text))
        self._save()

        await self._client.query(user_text)

        buf = ""
        full_text = ""
        seen_text = ""

        try:
            async for msg in self._client.receive_response():
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
                sentences, buf = emit_sentences(buf, min_len=40, force_flush=False)
                for s in sentences:
                    yield s

            sentences, buf = emit_sentences(buf, min_len=1, force_flush=True)
            for s in sentences:
                yield s
        finally:
            tail = (full_text + buf).strip()
            if tail:
                self.history.append(Turn(role="assistant", content=tail))
                self._save()

    async def interrupt(self) -> None:
        """Abort the current Claude generation (used for barge-in)."""
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
        self.history = [Turn(**d) for d in data]
        log.info("loaded %d turns from %s", len(self.history), path)

    def _save(self) -> None:
        if self.session_path is None:
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [t.__dict__ for t in self.history]
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
