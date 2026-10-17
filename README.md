# WriteAlong v2 — Dictation Studio

Dictate from a text file or PDF by ear, with pauses that behave like a real
person reading aloud. Built for handwriting practice and study sessions: you
follow along on the page, WriteAlong reads it to you, and the timing tries to
match what a human narrator would actually do.

Everything runs **offline**. No API keys, no network calls, no telemetry.

## The problem

Screen readers and TTS APIs read at a flat, mechanical rate. Long words get the
same airtime as short ones, a comma gets the same pause as a full stop, and the
result is tiring to follow by hand — you lose the thread of where you are on
the page faster than the narrator does.

WriteAlong models the pause instead of guessing at it.

## The pause engine

`GapEngine.breakdown()` computes a per-word delay rather than using a fixed
rate. The delay is a sum of independent terms, then clamped and scaled:

```python
raw   = think + write + syl_extra + extra + punct
total = clamp(raw * pace, MIN_GAP, MAX_GAP)      # 1.1s .. 12.0s
```

| Term | What it models |
|---|---|
| `think` | Latency before speaking — the narrator deciding to start |
| `write` | Word length × speed-per-letter × a **length curve** (`1 + 0.08·(L-6)`), so long words cost superlinearly |
| `syl_extra` | `0.12` per syllable beyond the first, estimated by vowel-group counting |
| `extra` | Articulation cost: digits `+0.5`, ALL-CAPS `+0.4`, hyphenated/camelCase `+0.3` |
| `punct` | Clause `+0.8`, sentence end `+1.6`, sentence-initial lead `+0.4`, page end `+0.6` |

A common-word allowlist suppresses the long-word penalty for words the narrator
wouldn't stumble on.

Because every term is returned in a dict, the UI can show *why* a gap is the
length it is — the breakdown is inspectable rather than magic.

### Punctuation is never silent

Tokens that clean down to nothing (`,`, `.`, `—`, `___`) would otherwise be
skipped and read as a dropped word. `spoken_form()` maps them to spoken names
instead, so a run of underscores is narrated as "blank" and a dash as "em
dash".

## Features

- **`.txt` and `.pdf` input** — file picker or drag and drop
- **Click any word to jump** — live page text beside a rendered page image
- **Handwriting calibration** — presets tuned for how you write, not how fast a
  machine can speak
- **Pace modifier**, **hold-to-extend**, and **sentence loop** for drilling a
  single sentence
- **Material 3 dark theme**, hand-built from Google's tonal colour roles,
  8dp spacing rhythm and type scale — no UI toolkit required
- Graceful degradation: drag-and-drop, PDF rendering and image support are each
  optional and detected at import time
- **Browser variant** (`index.html`) via pdf.js, if you'd rather not install
  anything

## Install

Requires Python 3.10+ and Tk. On Fedora:

```bash
sudo dnf install python3-tkinter piper-tts piper-tts-model-en_US-lessac-medium
```

Or from PyPI, plus a Piper voice:

```bash
pip install piper-tts piper-phonem-cross pygame pypdf pymupdf pillow
```

Download a voice and point `App` at it (see `MODEL` in `dictation_gui.py`).
The default is `en_US-lessac-medium`.

```bash
python3 dictation_gui.py                # empty, then pick a file
python3 dictation_gui.py notes.pdf      # or open one directly
```

The model weights (~63 MB) are **not** committed here — grab them from the
[Piper voices release](https://github.com/rhasspy/piper/blob/master/VOICES.md)
to keep the repo light.

## Layout

```text
.
├── dictation_gui.py       # app: GapEngine, Doc, App, Material 3 theme
├── pdf_dictate_auto.py    # deprecated shim, kept so old habits still run
└── index.html             # browser variant (pdf.js)
```

`dictation_gui.py` is the whole program — no build step, no framework.

## Status

v2, works. This is a personal tool that grew out of needing to practise
handwriting from printed material, and it does that reliably.

## License

MIT.
