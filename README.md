# airprompt — hands-free voice conversation with Claude Opus

Low-latency, continuous-listen voice app that uses Claude Opus 4.7 as a mock interviewer.
All speech models are open source and run locally on GPU. The LLM uses the Claude
Agent SDK piggybacking on your existing `claude` CLI auth (Pro subscription works).

## Stack

| Layer | Choice |
|---|---|
| STT  | `faster-whisper` large-v3 (fp16, CUDA) |
| TTS  | Kokoro-82M (`kokoro` PyPI) |
| VAD  | Silero VAD (`silero-vad` PyPI) |
| LLM  | `claude-agent-sdk` → Claude Opus 4.7 |
| Audio I/O | `sounddevice` + PortAudio |

## One-time setup

```bash
# 1. system packages
sudo apt update
sudo apt install -y python3.10 portaudio19-dev ffmpeg espeak-ng libsndfile1

# 2. claude CLI (Agent SDK shells out to it; OAuth gives you Pro auth)
curl -fsSL https://claude.ai/install.sh | bash
claude login   # follow browser flow

# 3. python venv + deps (using uv)
uv sync
```

This creates `.venv/`, resolves deps from `pyproject.toml`, writes `uv.lock`,
and editable-installs the project.

First run downloads the models (~2 GB total) into `~/.cache/`.

## Run

```bash
uv run python -m airprompt --role "Python backend engineer"
```

Or activate the venv first if you prefer:

```bash
source .venv/bin/activate
python -m airprompt --role "Python backend engineer"
```

Speak when you see `[ready]`. The mic listens continuously; talking ends when you pause
for ~400 ms. Ctrl+C to end.

### Resume a prior session

```bash
python -m airprompt --resume ~/.local/share/airprompt/sessions/session-20260509-101500.json
```

### Other flags

```
--list-devices            list audio devices
--input-device N          pick mic by index
--output-device N         pick speaker by index
--log-level DEBUG         verbose logging
```

## Smoke tests

Before running the full loop, sanity-check each component:

```bash
# TTS
python -m airprompt.tts "Hello, this is a Kokoro test."

# STT (record yourself: ffmpeg -f alsa -i default -t 5 -ar 16000 -ac 1 me.wav)
python -m airprompt.stt me.wav
```

## Project layout

```
src/airprompt/
  audio_io.py     mic/speaker streams + thread-asyncio bridge
  vad.py          Silero VAD endpointing
  stt.py          faster-whisper
  tts.py          Kokoro
  llm.py          Agent SDK + sentence splitter + persona + history
  orchestrator.py state machine + queues + tasks
  main.py         CLI entry
```

## Latency notes

End-to-end target (mic stops → first speaker sample): **~1.3–1.8 s** for short turns.
Long replies start playing while Claude is still generating because the LLM stream is
sentence-split and piped to TTS chunk-by-chunk.

## Tuning (env vars)

All read at startup; export before launching.

| Var | Default | Meaning |
|---|---|---|
| `AIRPROMPT_SILENCE_MS` | `800` | Silence (ms) that ends your turn |
| `AIRPROMPT_VAD_START_THRESHOLD` | `0.5` | Speech-start probability (0-1) |
| `AIRPROMPT_VAD_END_THRESHOLD` | `0.35` | Below this prob counts as silence |
| `AIRPROMPT_VAD_START_FRAMES` | `3` | Consecutive frames to confirm start (~32 ms each) |
| `AIRPROMPT_BARGE_THRESHOLD` | `0.7` | Higher start threshold while assistant speaks |
| `AIRPROMPT_BARGE_FRAMES` | `5` | Consecutive frames to confirm barge-in |
| `AIRPROMPT_PREROLL_MS` | `300` | Audio captured before speech-start (no clipped phonemes) |
| `AIRPROMPT_MIN_UTTERANCE_MS` | `250` | Drop utterances shorter than this |

Examples:
```bash
# Slower turn-taking — waits 1.5 s of silence before ending your turn
AIRPROMPT_SILENCE_MS=1500 python -m airprompt --role "data scientist"

# Less twitchy barge-in (need 250 ms of confirmed speech to interrupt)
AIRPROMPT_BARGE_FRAMES=8 AIRPROMPT_BARGE_THRESHOLD=0.75 python -m airprompt
```

## Barge-in

True barge-in is on by default: the mic is always live, and starting to speak
while Claude is talking will cancel the current reply and let you take over. For
best results, **use headphones** — speaker bleed-through into the mic can
trigger spurious interrupts. If you must use speakers, raise `AIRPROMPT_BARGE_THRESHOLD`
to ~0.8 and `AIRPROMPT_BARGE_FRAMES` to ~8.

## Known limits (v1)

- Single-language (English). Change `language="en"` in `stt.py` and `lang_code="a"` in
  `tts.py` to switch.
- No acoustic echo cancellation — barge-in relies on a higher VAD threshold during
  TTS playback. Headphones recommended.
- Pro subscription has session/weekly caps. If you hit them, set `ANTHROPIC_API_KEY`
  and the SDK falls back to API-key auth automatically.
