# airprompt — hands-free voice conversation with Claude Opus

Low-latency, continuous-listen voice app that uses Claude Opus 4.7. The persona
is driven by **user-editable personality presets** (interviewer, tutor, debate
partner, …) and can be grounded in **attached personal files** (resume,
portfolio brief, study notes — `.txt` / `.md` / `.pdf`).

All speech models are open source and run locally on GPU. The LLM uses the Claude
Agent SDK piggybacking on your existing `claude` CLI auth (Pro subscription works).

> Want to understand how the code flows? See [docs/FLOW.md](docs/FLOW.md) — four diagrams (architecture, state machine, sequence, queue topology) that get you to a working mental model in ~15 minutes.

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

### Attach personal files

Pass `--attach` (repeatable) to give the persona context about you. Supported
formats: `.txt`, `.md`, `.pdf`.

```bash
# Mock interview grounded in your resume
python -m airprompt --role "Backend Engineer" --attach resume=~/docs/cv.pdf

# Multiple files with optional labels (label= prefix)
python -m airprompt \
  --attach resume=~/docs/cv.pdf \
  --attach portfolio=~/docs/projects.md \
  --attach ~/docs/notes.txt        # bare path → label is the file stem
```

The interviewer (or any personality whose template references
`{attachments_section}`) will probe specific projects, roles, and timelines from
the file rather than asking generic questions. Files are capped at ~8k chars
each, ~24k cumulative — long PDFs get truncated with a warning.

### Personalities (presets)

The default `interviewer` preset is auto-installed on first run to:

```
~/.config/airprompt/personalities/interviewer.md
```

Edit that file to tune the interviewer, or drop in a new `<name>.md` to define a
new personality and select it with `--personality <name>`. Each file is markdown
with YAML frontmatter:

```markdown
---
name: tutor
description: Patient tutor that adapts to the learner's level
uses_attachments: true       # set false if this persona doesn't use --attach
defaults:
  effort: xhigh              # passed to the Agent SDK
---
You are a patient tutor for {role}.

{attachments_section}

Ask one question at a time, ...
```

Two placeholders are available in the template body:

| Placeholder | Filled with |
|---|---|
| `{role}` | The `--role` argument (free-form string) |
| `{attachments_section}` | Demarcated, labeled blocks of any `--attach` files. Empty string when none. |

List what's installed:

```bash
python -m airprompt --list-personalities
```

### Resume a prior session

```bash
python -m airprompt --continue ~/.local/share/airprompt/sessions/session-20260509-101500.json
```

The session file remembers the `personality`, `role`, and `--attach` paths used
when it was created. By default `--continue` re-uses all of them, so the bot
keeps its full context (including your resume) without re-typing flags. Pass
`--personality`, `--role`, or `--attach` explicitly to override individual
values for the resumed session.

(`--resume` still works as a deprecated alias for one release; prefer `--continue`.)

### Feedback reports

Generate a personality-driven written debrief of a session — for the interviewer
this covers strengths, weaknesses, communication (grammar / fluency / clarity),
the single most important area to work on, and concrete practical advice. The
template explicitly tells the model to **omit any section it cannot back with
real evidence**, so honest short reports are preferred over padded ones.

Two ways to use it:

```bash
# Live: speak the feedback at the end of the session AND save it to disk.
# Trigger by Ctrl+C when you're done talking.
python -m airprompt --feedback --role "Backend Engineer"

# Post-hoc: generate a feedback file from any saved session JSON. Headless —
# no mic, no TTS, no orchestrator. Useful for reviewing old sessions.
python -m airprompt --feedback-from ~/.local/share/airprompt/sessions/session-20260509-101500.json

# Both modes accept --feedback-out to choose the output path.
python -m airprompt --feedback-from <session.json> --feedback-out ~/reports/today.md
```

Default output location: `~/.local/share/airprompt/feedback/feedback-{YYYYMMDD-HHMMSS}.md`.
Each file gets a small HTML-comment header noting personality, role, source
session, and generation time.

`--feedback-from` is mutually exclusive with the live-mode flags
(`--continue`, `--role`, `--attach`, `--input-device`, `--output-device`,
`--feedback`) — post-hoc reads everything from the saved session. You may
combine it with `--personality` to apply a different persona's feedback
template against the saved transcript.

The feedback template is part of the personality file. To enable feedback for a
custom personality, add a `feedback:` block to its YAML frontmatter:

```markdown
---
name: tutor
defaults:
  effort: xhigh
feedback:
  enabled: true
  template: |
    The tutoring session for {role} just ended after {turn_count} turns.

    Transcript:
    {transcript}

    Write a candid debrief... (use markdown, omit empty sections, etc.)
---
```

Available placeholders in the feedback template: `{role}`, `{transcript}`,
`{turn_count}`, `{attachments_section}`, `{personality_name}`, `{started_at}`,
`{ended_at}`. A personality without a `feedback:` block raises a clear error
when feedback is requested rather than falling back to a generic template.


### Other flags

```
--personality NAME        personality preset (default: interviewer)
--role STR                role/topic injected into the personality template
--attach [LABEL=]PATH     attach a personal file; repeatable
--continue PATH           resume a prior session JSON
--feedback                live mode: speak + save feedback at session end
--feedback-from PATH      post-hoc: generate feedback for a saved session JSON
--feedback-out PATH       where to write feedback markdown (default: ~/.local/share/airprompt/feedback/)
--list-personalities      list available personalities and exit
--list-devices            list audio devices
--input-device N          pick mic by index
--output-device N         pick speaker by index
--log-level DEBUG         verbose logging (use DEBUG to see the rendered system prompt)
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
  audio_io.py        mic/speaker streams + thread-asyncio bridge
  vad.py             Silero VAD endpointing
  stt.py             faster-whisper
  tts.py             Kokoro
  llm.py             Agent SDK + sentence splitter + persona + history
  personality.py     load / parse / render personality presets
  attachments.py     load .txt/.md/.pdf into the prompt
  feedback.py        post-hoc + live feedback report generation
  personalities/     packaged default presets (auto-copied to ~/.config/airprompt/)
    interviewer.md
  orchestrator.py    state machine + queues + tasks
  main.py            CLI entry
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
