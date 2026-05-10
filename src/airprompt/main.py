"""CLI entry point.

Examples:
    python -m airprompt
    python -m airprompt --role "AI Engineer"
    python -m airprompt --personality interviewer --role "Backend" --attach resume=cv.pdf
    python -m airprompt --continue ~/.local/share/airprompt/sessions/session-20260509-101500.json
    python -m airprompt --feedback --role "Backend Engineer"
    python -m airprompt --feedback-from ~/.local/share/airprompt/sessions/session-XXX.json
    python -m airprompt --list-personalities
    python -m airprompt --list-devices
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .orchestrator import Orchestrator
from .personality import bootstrap_user_dir, describe_all


def _load_saved_session(path: Path) -> dict:
    """Pull personality / role / attachments out of a saved session, if present.

    Older session files were a flat list of turns and carry no metadata.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _list_devices() -> None:
    import sounddevice as sd

    print(sd.query_devices())


def _list_personalities() -> None:
    bootstrap_user_dir()
    rows = describe_all()
    if not rows:
        print("(no personalities found)")
        return
    width = max(len(name) for name, _ in rows)
    for name, desc in rows:
        print(f"{name.ljust(width)}  {desc}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="airprompt")
    parser.add_argument("--personality", default=None, help="Personality preset name (default: interviewer, or value from --continue session)")
    parser.add_argument("--role", default=None, help="Role/topic passed to the personality template (default: 'AI Engineer', or value from --continue session)")
    parser.add_argument(
        "--attach",
        action="append",
        default=None,
        metavar="[LABEL=]PATH",
        help="Attach a personal file (.txt/.md/.pdf). Repeatable. Optional 'label=' prefix. With --continue, omit to reuse the saved attachments.",
    )
    parser.add_argument(
        "--continue",
        dest="continue_path",
        type=Path,
        default=None,
        help="Path to a prior session JSON to continue",
    )
    parser.add_argument(
        "--resume",
        dest="resume_path_deprecated",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--input-device", type=int, default=None, help="sounddevice input device index")
    parser.add_argument("--output-device", type=int, default=None, help="sounddevice output device index")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--list-personalities", action="store_true", help="List personalities and exit")
    parser.add_argument(
        "--feedback",
        action="store_true",
        help="Generate, speak, and save personality-driven feedback at the end of a live session (Ctrl+C).",
    )
    parser.add_argument(
        "--feedback-from",
        dest="feedback_from",
        type=Path,
        default=None,
        metavar="SESSION.json",
        help="Headless: read a saved session JSON, generate Markdown feedback, and exit. No mic/TTS.",
    )
    parser.add_argument(
        "--feedback-out",
        dest="feedback_out",
        type=Path,
        default=None,
        metavar="PATH",
        help="Where to write the feedback Markdown. Default: ~/.local/share/airprompt/feedback/feedback-{ts}.md",
    )
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING"])
    args = parser.parse_args()

    # --feedback-from is mutually exclusive with live-mode flags (it's headless).
    if args.feedback_from is not None:
        conflicting = []
        if args.continue_path is not None:
            conflicting.append("--continue")
        if args.role is not None:
            conflicting.append("--role")
        if args.attach is not None:
            conflicting.append("--attach")
        if args.input_device is not None:
            conflicting.append("--input-device")
        if args.output_device is not None:
            conflicting.append("--output-device")
        if args.feedback:
            conflicting.append("--feedback")
        if conflicting:
            parser.error(
                f"--feedback-from cannot be combined with: {', '.join(conflicting)} "
                "(post-hoc feedback reads everything from the saved session)"
            )

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.list_devices:
        _list_devices()
        return
    if args.list_personalities:
        _list_personalities()
        return

    if args.feedback_from is not None:
        from . import feedback as feedback_mod

        try:
            out = asyncio.run(
                feedback_mod.run_post_hoc(
                    session_path=args.feedback_from.expanduser(),
                    out_path=args.feedback_out,
                    personality_override=args.personality,
                )
            )
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"\n\033[32m[feedback]\033[0m wrote {out}")
        return

    continue_path = args.continue_path
    if args.resume_path_deprecated is not None:
        print(
            "warning: --resume is deprecated; use --continue for session resume.",
            file=sys.stderr,
        )
        continue_path = continue_path or args.resume_path_deprecated

    saved = _load_saved_session(continue_path) if continue_path else {}
    personality_name = args.personality or saved.get("personality") or "interviewer"
    role = args.role or saved.get("role") or "AI Engineer"
    if args.attach is not None:
        attach_specs = args.attach
    else:
        attach_specs = [
            f"{a['label']}={a['path']}" for a in saved.get("attachments", [])
        ]

    orch = Orchestrator(
        personality_name=personality_name,
        role=role,
        attach_specs=attach_specs,
        continue_path=continue_path,
        input_device=args.input_device,
        output_device=args.output_device,
        feedback_enabled=args.feedback,
        feedback_out_path=args.feedback_out,
    )
    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
