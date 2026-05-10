"""User-editable personality presets.

A personality is a markdown file with YAML frontmatter:

    ---
    name: interviewer
    description: Mock job interview tailored to a target role
    uses_attachments: true
    defaults:
      effort: xhigh
    ---
    You are conducting a mock interview for {role}.
    {attachments_section}
    ...

User presets live at ~/.config/airprompt/personalities/. Packaged defaults ship
with the wheel and are copied into the user dir on first run.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

log = logging.getLogger(__name__)

USER_DIR = Path.home() / ".config/airprompt/personalities"
PACKAGE_DIR = Path(__file__).parent / "personalities"


class PersonalityError(Exception):
    pass


class PersonalityNotFound(PersonalityError):
    pass


@dataclass
class Personality:
    name: str
    description: str
    uses_attachments: bool
    prompt_template: str
    defaults: dict = field(default_factory=dict)
    feedback: dict | None = None


_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.DOTALL)


def _parse(text: str, source: Path) -> Personality:
    if not text.startswith("---"):
        raise PersonalityError(f"{source}: missing YAML frontmatter delimited by '---'")
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        raise PersonalityError(f"{source}: malformed frontmatter (need opening and closing '---' on their own lines)")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).lstrip("\n")
    name = meta.get("name") or source.stem
    feedback = meta.get("feedback")
    if feedback is not None and not isinstance(feedback, dict):
        raise PersonalityError(f"{source}: 'feedback' must be a mapping if present")
    return Personality(
        name=name,
        description=meta.get("description", ""),
        uses_attachments=bool(meta.get("uses_attachments", False)),
        prompt_template=body,
        defaults=meta.get("defaults") or {},
        feedback=feedback,
    )


def _candidate_paths(name: str) -> Iterable[Path]:
    yield USER_DIR / f"{name}.md"
    yield PACKAGE_DIR / f"{name}.md"


def load(name: str) -> Personality:
    for path in _candidate_paths(name):
        if path.exists():
            log.info("loading personality %r from %s", name, path)
            return _parse(path.read_text(), path)
    available = ", ".join(sorted(list_available())) or "(none)"
    raise PersonalityNotFound(f"personality {name!r} not found. available: {available}")


def list_available() -> list[str]:
    names: set[str] = set()
    for d in (USER_DIR, PACKAGE_DIR):
        if d.exists():
            for p in d.glob("*.md"):
                names.add(p.stem)
    return sorted(names)


def describe_all() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name in list_available():
        try:
            p = load(name)
            out.append((p.name, p.description))
        except PersonalityError as e:
            out.append((name, f"(failed to load: {e})"))
    return out


def render(p: Personality, role: str, attachments_section: str) -> str:
    if p.uses_attachments and not attachments_section:
        log.warning(
            "personality %r expects attachments but none were provided", p.name
        )
    mapping = {"role": role, "attachments_section": attachments_section}
    try:
        return p.prompt_template.format_map(_SafeMap(mapping))
    except (KeyError, IndexError) as e:
        raise PersonalityError(f"failed to render {p.name!r}: {e}") from e


def has_feedback(p: Personality) -> bool:
    if not p.feedback:
        return False
    if not p.feedback.get("template"):
        return False
    return bool(p.feedback.get("enabled", True))


def render_feedback(
    p: Personality,
    *,
    role: str,
    attachments_section: str,
    transcript: str,
    turn_count: int,
    started_at: str,
    ended_at: str,
) -> str:
    if not has_feedback(p):
        raise PersonalityError(
            f"personality {p.name!r} has no feedback template (or it's disabled)"
        )
    template = p.feedback["template"]
    mapping = {
        "role": role,
        "attachments_section": attachments_section,
        "transcript": transcript,
        "turn_count": str(turn_count),
        "personality_name": p.name,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    try:
        return template.format_map(_SafeMap(mapping))
    except (KeyError, IndexError) as e:
        raise PersonalityError(
            f"failed to render feedback template for {p.name!r}: {e}"
        ) from e


class _SafeMap(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def bootstrap_user_dir() -> None:
    """Copy packaged defaults into USER_DIR if the user hasn't customized yet."""
    USER_DIR.mkdir(parents=True, exist_ok=True)
    if not PACKAGE_DIR.exists():
        return
    for src in PACKAGE_DIR.glob("*.md"):
        dst = USER_DIR / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            log.info("installed default personality %s", dst)
