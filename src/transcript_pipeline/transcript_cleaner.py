"""Deterministic noise removal for Notion AI Meeting transcripts.

Strips transcription artefacts (per-utterance block splitting, bare
timestamps, empty speaker labels, sentence doubling) before the
transcript reaches the LLM. Pattern-based only — no NLP, no content
pruning. If a transcript doesn't match the expected shape, the cleaner
is a no-op for that rule rather than damaging the text.

Two layers, both shipped together (see plan in chat history):

* Layer A (zero-risk):
    - Trim whitespace, collapse blank-line runs.
    - Drop pure-timestamp lines (e.g. ``[00:01:23]`` or ``00:01:23 -->``).
    - Drop bare speaker labels with no content (``Santiago:``).

* Layer B (low-risk):
    - Merge consecutive same-speaker utterances onto one line.
    - Drop adjacent identical sentences within a single utterance.

Filler removal and "repeat-then-restart" collapsing are intentionally
out of scope until shadow-diff validates them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Speaker labels we recognise: Notion's anonymous "Speaker N" form, or
# 1-4 capitalised name tokens. Conservative on purpose — we'd rather miss
# a label than misclassify a content line ending in ':' as a speaker.
_SPEAKER_LABEL = re.compile(
    r"^(?P<speaker>"
    r"Speaker\s+\d+"
    r"|(?:[A-ZÀ-Þ][A-Za-zÀ-ÿ\.\'-]+)(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÿ\.\'-]+){0,3}"
    r"):\s*(?P<rest>.*)$"
)

# A standalone timestamp line: "[00:01:23]", "00:01", "00:01:23 --> 00:02:10".
_TIMESTAMP_ONLY = re.compile(
    r"^\s*\[?\d{1,2}:\d{2}(?::\d{2})?\]?"
    r"(?:\s*[-–—>→]+\s*\[?\d{1,2}:\d{2}(?::\d{2})?\]?)?\s*$"
)

# Sentence boundary heuristic for in-line dedup: split on ?, !, . followed
# by whitespace. Good enough for adjacent-identical detection; not used
# for any semantic decision.
_SENTENCE_SPLIT = re.compile(r"(?<=[\.\?\!])\s+")


@dataclass(frozen=True)
class CleanResult:
    """Cleaned transcript plus before/after metrics for logging."""

    text: str
    chars_before: int
    chars_after: int

    @property
    def ratio(self) -> float:
        if self.chars_before == 0:
            return 0.0
        return self.chars_after / self.chars_before


def _split_speaker(line: str) -> tuple[str | None, str]:
    """Return (speaker, rest) for a speaker-prefixed line; else (None, line)."""
    m = _SPEAKER_LABEL.match(line)
    if not m:
        return None, line
    return m.group("speaker").strip(), m.group("rest").strip()


def _layer_a(text: str) -> list[str]:
    """Apply zero-risk cleanups, return list of kept lines (blanks preserved)."""
    kept: list[str] = []
    prev_blank = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if not prev_blank:
                kept.append("")
                prev_blank = True
            continue
        prev_blank = False

        if _TIMESTAMP_ONLY.match(line):
            continue

        speaker, rest = _split_speaker(line)
        if speaker is not None and not rest:
            continue

        kept.append(line)

    while kept and not kept[0]:
        kept.pop(0)
    while kept and not kept[-1]:
        kept.pop()
    return kept


def _dedupe_adjacent_sentences(body: str) -> str:
    """Collapse adjacent identical sentences within a single utterance body."""
    if not body:
        return body
    parts = _SENTENCE_SPLIT.split(body)
    out: list[str] = []
    last_norm: str | None = None
    for p in parts:
        norm = p.strip().lower()
        if norm and norm == last_norm:
            continue
        out.append(p)
        last_norm = norm
    return " ".join(s for s in (p.strip() for p in out) if s)


def _layer_b(lines: list[str]) -> list[str]:
    """Merge same-speaker runs, dedupe adjacent identical sentences per run."""
    merged: list[str] = []
    pending_speaker: str | None = None
    pending_parts: list[str] = []

    def flush() -> None:
        nonlocal pending_speaker, pending_parts
        if pending_speaker is None:
            return
        body = " ".join(p for p in pending_parts if p)
        body = _dedupe_adjacent_sentences(body)
        merged.append(f"{pending_speaker}: {body}".rstrip())
        pending_speaker = None
        pending_parts = []

    for line in lines:
        if not line:
            flush()
            merged.append("")
            continue

        speaker, rest = _split_speaker(line)
        if speaker is not None:
            if pending_speaker == speaker:
                pending_parts.append(rest)
            else:
                flush()
                pending_speaker = speaker
                pending_parts = [rest]
        else:
            # Continuation of pending speaker if any, else free-standing line.
            if pending_speaker is not None:
                pending_parts.append(line)
            else:
                merged.append(line)
    flush()
    return merged


def clean(transcript: str) -> CleanResult:
    """Apply Layer A + Layer B cleanup, return text plus before/after sizes."""
    chars_before = len(transcript or "")
    if not transcript:
        return CleanResult(text=transcript or "", chars_before=0, chars_after=0)

    lines = _layer_a(transcript)
    lines = _layer_b(lines)

    out: list[str] = []
    prev_blank = False
    for line in lines:
        if not line:
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(line)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()

    text = "\n".join(out)
    return CleanResult(text=text, chars_before=chars_before, chars_after=len(text))
