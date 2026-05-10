"""Post-interview feedback generation.

Two entry points share the same prompt-rendering and file-writing helpers:

* Live mode is wired into the orchestrator (see `Orchestrator._run_feedback_turn`)
  and reuses an open `InterviewerLLM` to stream + speak the feedback.
* Post-hoc mode (`run_post_hoc`) is fully headless: it loads a saved session
  JSON, opens a fresh one-shot Claude client, and writes Markdown to disk.
  It does not touch any audio / orchestrator code.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Iterable

from .attachments import Attachment, load_attachment, render_attachments_section
from .personality import (
    Personality,
    PersonalityError,
    bootstrap_user_dir,
    has_feedback,
    load as load_personality,
    render as render_personality,
    render_feedback,
)

log = logging.getLogger(__name__)

FEEDBACK_DIR = Path.home() / ".local/share/airprompt/feedback"
MODEL = "claude-opus-4-7"


def default_feedback_path() -> Path:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return FEEDBACK_DIR / f"feedback-{time.strftime('%Y%m%d-%H%M%S')}.md"


def load_saved_session(path: Path) -> dict:
    """Pull personality / role / attachments / turns out of a saved session."""
    if not path.exists():
        raise FileNotFoundError(f"session file not found: {path}")
    data = json.loads(path.read_text())
    if isinstance(data, list):
        # Legacy: flat list of turns, no metadata.
        return {"turns": data}
    return data


def format_transcript(turns: Iterable[dict]) -> str:
    """Render saved-session turns as 'Candidate: ...' / 'Interviewer: ...' lines."""
    lines = []
    for t in turns:
        role = t.get("role", "user")
        speaker = "Candidate" if role == "user" else "Interviewer"
        content = (t.get("content") or "").strip()
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def _ts_to_iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def render_feedback_prompt(
    p: Personality,
    *,
    role: str,
    attachments_section: str,
    turns: list[dict],
) -> str:
    """Build the user-message prompt to send to Claude for feedback generation.

    Both live and post-hoc paths funnel through here so the wording matches.
    """
    transcript = format_transcript(turns)
    started = _ts_to_iso(turns[0].get("ts") if turns else None)
    ended = _ts_to_iso(turns[-1].get("ts") if turns else None)
    return render_feedback(
        p,
        role=role,
        attachments_section=attachments_section,
        transcript=transcript,
        turn_count=len(turns),
        started_at=started,
        ended_at=ended,
    )


def write_feedback_file(
    out_path: Path,
    body_markdown: str,
    *,
    personality_name: str,
    role: str,
    source_session: Path | None,
) -> Path:
    out_path = out_path.expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header_lines = [
        "<!--",
        f"  airprompt feedback report",
        f"  personality: {personality_name}",
        f"  role: {role}",
        f"  source session: {source_session if source_session else '(live session)'}",
        f"  generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        "-->",
        "",
    ]
    out_path.write_text("\n".join(header_lines) + body_markdown.strip() + "\n")
    return out_path


async def _one_shot_query(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    effort: str,
) -> str:
    """Open a fresh Claude client, send one prompt, accumulate the full reply."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
    )

    options = ClaudeAgentOptions(
        model=model,
        effort=effort,
        system_prompt=system_prompt,
    )
    full_text = ""
    seen = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                pieces = []
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        pieces.append(block.text)
                msg_text = "".join(pieces)
                # Some SDK versions deliver running cumulative text; others deliver deltas.
                if msg_text.startswith(seen):
                    full_text += msg_text[len(seen):]
                    seen = msg_text
                else:
                    full_text += msg_text
                    seen = msg_text
    return full_text.strip()


def _load_attachments_best_effort(saved_attachments: list[dict]) -> list[Attachment]:
    out: list[Attachment] = []
    for a in saved_attachments:
        path = a.get("path")
        if not path:
            continue
        try:
            spec = f"{a['label']}={path}" if a.get("label") else str(path)
            out.append(load_attachment(spec))
        except Exception as e:  # noqa: BLE001
            log.warning("skipping attachment %s for feedback (%s)", path, e)
    return out


async def run_post_hoc(
    session_path: Path,
    out_path: Path | None = None,
    personality_override: str | None = None,
) -> Path:
    """Generate feedback Markdown for a saved session. Returns the output path."""
    bootstrap_user_dir()
    saved = load_saved_session(session_path)

    personality_name = (
        personality_override
        or saved.get("personality")
        or "interviewer"
    )
    role = saved.get("role") or "(unspecified role)"
    turns = saved.get("turns") or []

    p = load_personality(personality_name)
    if not has_feedback(p):
        raise PersonalityError(
            f"personality {p.name!r} has no feedback template "
            f"(add a `feedback:` block to its frontmatter)"
        )

    attachments = _load_attachments_best_effort(saved.get("attachments") or [])
    attachments_section = render_attachments_section(attachments)

    if not turns:
        markdown = (
            "_The transcript is empty — there is no candidate dialog to "
            "evaluate. Run an actual conversation first._"
        )
    else:
        system_prompt = render_personality(p, role, attachments_section)
        user_prompt = render_feedback_prompt(
            p, role=role, attachments_section=attachments_section, turns=turns
        )
        effort = (p.feedback or {}).get("effort") or p.defaults.get("effort", "xhigh")
        model = p.defaults.get("model", MODEL)
        log.info(
            "generating post-hoc feedback (personality=%s, role=%s, turns=%d, model=%s, effort=%s)",
            p.name, role, len(turns), model, effort,
        )
        markdown = await _one_shot_query(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            effort=effort,
        )

    final_out = out_path.expanduser() if out_path else default_feedback_path()
    return write_feedback_file(
        final_out,
        markdown,
        personality_name=p.name,
        role=role,
        source_session=session_path,
    )
