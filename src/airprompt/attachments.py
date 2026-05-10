"""Personal context files attached at startup.

Users pass --attach repeatedly; each spec is `label=path` or bare `path`.
Supported extensions: .txt, .md, .pdf. Content is rendered into the personality
prompt under a single `{attachments_section}` placeholder.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PER_FILE_CHAR_CAP = 8000
TOTAL_CHAR_CAP = 24000

SUPPORTED_EXTS = (".txt", ".md", ".pdf")


@dataclass
class Attachment:
    label: str
    path: Path
    text: str


def parse_attach_arg(raw: str) -> tuple[str | None, Path]:
    if "=" in raw:
        label, _, p = raw.partition("=")
        label = label.strip() or None
        return label, Path(p).expanduser()
    return None, Path(raw).expanduser()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            log.exception("pdf page extract_text failed in %s", path)
            pages.append("")
    return "\n\n".join(pages)


def load_attachment(spec: str) -> Attachment:
    label, path = parse_attach_arg(spec)
    if not path.exists():
        raise FileNotFoundError(f"attachment not found: {path}")
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        text = _read_text(path)
    elif ext == ".pdf":
        text = _read_pdf(path)
    else:
        raise ValueError(
            f"unsupported attachment extension {ext!r}. supported: {', '.join(SUPPORTED_EXTS)}"
        )
    text = text.strip()
    if len(text) > PER_FILE_CHAR_CAP:
        log.warning(
            "attachment %s truncated from %d to %d chars",
            path, len(text), PER_FILE_CHAR_CAP,
        )
        text = text[:PER_FILE_CHAR_CAP] + "\n…[truncated]"
    final_label = label or path.stem
    log.info("attachment loaded: label=%s path=%s chars=%d", final_label, path, len(text))
    return Attachment(label=final_label, path=path, text=text)


def render_attachments_section(items: list[Attachment]) -> str:
    if not items:
        return ""
    blocks = []
    total = 0
    for a in items:
        remaining = TOTAL_CHAR_CAP - total
        body = a.text
        if len(body) > remaining:
            log.warning(
                "attachment %s clipped to fit cumulative cap (%d chars remaining)",
                a.label, max(remaining, 0),
            )
            body = body[: max(remaining, 0)] + "\n…[truncated]"
        blocks.append(f"[{a.label}]\n{body}")
        total += len(body)
        if total >= TOTAL_CHAR_CAP:
            break
    body = "\n\n".join(blocks)
    return (
        "--- PERSONAL FILES PROVIDED BY THE USER ---\n\n"
        f"{body}\n\n"
        "--- END PERSONAL FILES ---\n\n"
        "Use these to ground the conversation in the user's actual context. "
        "Reference specifics (projects, roles, dates, names) when relevant."
    )
