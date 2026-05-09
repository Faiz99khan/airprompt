"""CLI entry point.

Examples:
    python -m airprompt
    python -m airprompt --role "AI Engineer"
    python -m airprompt --resume ~/.local/share/airprompt/sessions/session-20260509-101500.json
    python -m airprompt --list-devices
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .orchestrator import Orchestrator


def _list_devices() -> None:
    import sounddevice as sd

    print(sd.query_devices())


def main() -> None:
    parser = argparse.ArgumentParser(prog="airprompt")
    parser.add_argument("--role", default="AI Engineer", help="Interview role/position")
    parser.add_argument("--resume", type=Path, default=None, help="Path to a prior session JSON to resume")
    parser.add_argument("--input-device", type=int, default=None, help="sounddevice input device index")
    parser.add_argument("--output-device", type=int, default=None, help="sounddevice output device index")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING"])
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.list_devices:
        _list_devices()
        return

    orch = Orchestrator(
        role=args.role,
        resume_path=args.resume,
        input_device=args.input_device,
        output_device=args.output_device,
    )
    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
