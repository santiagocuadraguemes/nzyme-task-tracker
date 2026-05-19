"""Preview a transcript compression candidate without firing the merged extractor.

Pulls the raw transcript for a Notion meeting page, runs the existing regex
cleaner (Layer A+B), then applies a candidate compression pass on top.
Counts Gemini tokens before and after via the free count_tokens API and
writes the two transcript versions to preview/<page_id>.{before,after}.txt
so you can eyeball the diff.

Usage:
    ../venv/Scripts/python scripts/preview_compression.py <page_id>

Token counting hits Gemini's count_tokens endpoint, which is free and
unmetered (does not consume daily quota on the free tier). No merged-
extraction calls are made.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

# Make ``src`` importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from notion_client import Client as NotionClient  # noqa: E402

from src.notion_client_wrapper import NotionClientWrapper  # noqa: E402
from src.transcript_pipeline.fetch_transcript import fetch_transcript  # noqa: E402
from src.transcript_pipeline.transcript_cleaner import clean as run_cleaner  # noqa: E402


# ---------------------------------------------------------------------------
# C1 — filler / discourse-marker scrub (pure regex, no spaCy needed)
# ---------------------------------------------------------------------------
#
# Two sub-layers, both case-insensitive, both whole-word only:
#
#   C1a — single-token disfluencies. The classic "uh/um" set + Spanish
#         equivalents. Removed wherever they appear as standalone words.
#         These tokens carry no semantic load — they are by definition
#         place-holders the speaker uses while thinking.
#
#   C1b — clause-internal discourse markers (only between commas, or
#         between comma and end-of-sentence). "I'll send it, you know,
#         tomorrow" -> "I'll send it, tomorrow". Constrained to the
#         comma-bracketed form on purpose: "you know what I think" is
#         meaningful content, ", you know," is filler.
#
# Patterns deliberately omit ambiguous Spanish words ("este" can mean
# "this", "bueno" can mean "good") unless they appear in an unambiguous
# clause-internal position.

_C1A_FILLERS = [
    # English
    r"uh",
    r"uhh+",
    r"um",
    r"umm+",
    r"uhm",
    r"hmm+",
    r"mhm+",
    r"mm+",
    r"er",
    r"err+",
    r"ah",
    r"ahh+",
    r"eh",
    r"ehh+",
    # Spanish
    r"ehm+",
    r"mmm+",
    r"este+",  # only matched as standalone, see pattern below
]

_C1B_MARKERS = [
    # English
    r"you know",
    r"i mean",
    r"sort of",
    r"kind of",
    r"basically",
    r"actually",
    r"literally",
    r"like",
    r"right",
    # Spanish
    r"o sea",
    r"pues",
    r"bueno",
    r"digamos",
    r"a ver",
    r"mira",
    r"sabes",
    r"este",
]


def _scrub_c1a(text: str) -> tuple[str, Counter[str]]:
    """Drop single-token disfluencies. Returns (text, removal_counts)."""
    removed: Counter[str] = Counter()
    # Match a filler token surrounded by word boundaries, optionally followed
    # by a trailing comma or other punctuation. We replace with empty string
    # then tidy adjacent whitespace/punctuation afterwards.
    pattern = re.compile(
        r"\b(?P<tok>(?:" + "|".join(_C1A_FILLERS) + r"))\b[\s,]*",
        re.IGNORECASE,
    )

    def _sub(match: re.Match) -> str:
        removed[match.group("tok").lower()] += 1
        # Preserve a single space if we ate a separator; the cleanup pass
        # below collapses double-spaces.
        return " "

    out = pattern.sub(_sub, text)
    # Collapse the whitespace we may have introduced and tidy " ," / " ." etc.
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    # Restore line breaks: we collapsed \n too. Re-do this per-line instead.
    return out, removed


def _scrub_line_c1a(line: str) -> tuple[str, Counter[str]]:
    """C1a applied per-line so we don't flatten newlines."""
    pattern = re.compile(
        r"\b(?P<tok>(?:" + "|".join(_C1A_FILLERS) + r"))\b",
        re.IGNORECASE,
    )
    removed: Counter[str] = Counter()

    def _sub(match: re.Match) -> str:
        removed[match.group("tok").lower()] += 1
        return ""

    out = pattern.sub(_sub, line)
    # Tidy spacing/punct artefacts left behind.
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,;:])\s*([,;:])", r"\1", out)  # ", ," -> ","
    out = re.sub(r"^[\s,;:]+", "", out)  # leading filler punctuation after stripped utterance start
    return out.strip(), removed


def _scrub_line_c1b(line: str) -> tuple[str, Counter[str]]:
    """Drop clause-internal discourse markers (comma-bracketed only)."""
    pattern = re.compile(
        r",\s+(?P<tok>(?:" + "|".join(_C1B_MARKERS) + r"))\s*(?=,|\.|\!|\?|$)",
        re.IGNORECASE,
    )
    removed: Counter[str] = Counter()

    def _sub(match: re.Match) -> str:
        removed[match.group("tok").lower()] += 1
        return ""

    out = pattern.sub(_sub, line)
    out = re.sub(r"[ \t]+", " ", out)
    return out.strip(), removed


def apply_c1(text: str) -> tuple[str, Counter[str]]:
    """Run C1a + C1b line-by-line, return cleaned text + removal counts."""
    removed: Counter[str] = Counter()
    out_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            out_lines.append("")
            continue
        # Preserve the leading speaker label: only scrub the utterance body.
        m = re.match(r"^(?P<speaker>[^:]+:\s*)(?P<body>.*)$", line)
        if m and len(m.group("speaker")) < 60:
            head = m.group("speaker")
            body = m.group("body")
        else:
            head = ""
            body = line
        body_a, r_a = _scrub_line_c1a(body)
        body_b, r_b = _scrub_line_c1b(body_a)
        removed.update(r_a)
        removed.update(r_b)
        new_line = (head + body_b).rstrip()
        if new_line.strip().endswith(":"):
            # Filler-only utterance — drop it entirely.
            continue
        out_lines.append(new_line)
    return "\n".join(out_lines), removed


# ---------------------------------------------------------------------------
# C2 — signal-based sentence dropping (spaCy, Spanish-first)
# ---------------------------------------------------------------------------
#
# Each Notion transcript block is treated as one "turn". Within a turn we
# split into sentences via spaCy and score each one across four signals:
#
#   1. Named entities (any NER hit -> +1).
#   2. Commitment language (Imperative / simple future / periphrastic
#      future / obligation / intention -> +1).
#   3. Numeric or temporal anchor (NUM token or Spanish date wordlist -> +1).
#   4. Question marker (? or ¿ -> +1).
#
# A sentence is KEPT when:
#   - It is the first or last sentence of the turn (framing / closing
#     commitment guardrail), OR
#   - Its score ≥ 1.
#
# A sentence is DROPPED only when ALL four signals are silent AND it is
# not at a turn edge. This is the safest possible drop — pure
# conversational glue like "¿Vale?", "Sí, sí.", "Recibido.", "Bueno,
# nada." carries none of these signals and gets pruned.

_NLP_ES = None  # lazy-loaded


def _get_nlp_es():
    global _NLP_ES
    if _NLP_ES is None:
        import spacy

        _NLP_ES = spacy.load("es_core_news_sm")
    return _NLP_ES


# Spanish single-word temporal anchors. The small ES model doesn't tag
# these as DATE entities, so we compensate with a lexicon match.
_ES_DATE_WORDS = {
    "hoy", "ayer", "mañana", "anteayer",
    "lunes", "martes", "miércoles", "miercoles", "jueves",
    "viernes", "sábado", "sabado", "domingo",
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    "semana", "semanas", "mes", "meses", "trimestre", "trimestres",
    "año", "años", "ano", "anos", "día", "dia", "días", "dias",
    "hora", "horas", "minuto", "minutos",
}

_ES_DATE_PATTERNS = [
    re.compile(r"\bla\s+semana\s+que\s+viene\b", re.IGNORECASE),
    re.compile(r"\bla\s+pr[oó]xima\s+semana\b", re.IGNORECASE),
    re.compile(r"\bel\s+pr[oó]ximo\s+\w+\b", re.IGNORECASE),
    re.compile(r"\bpara\s+(?:el\s+|la\s+)?\w+", re.IGNORECASE),
    re.compile(r"\bantes\s+de\b", re.IGNORECASE),
    re.compile(r"\bhasta\s+(?:el\s+)?\w+", re.IGNORECASE),
    re.compile(r"\bdespu[eé]s\s+de\b", re.IGNORECASE),
    re.compile(r"\ba\s+finales\s+de\b", re.IGNORECASE),
    re.compile(r"\ba\s+principios\s+de\b", re.IGNORECASE),
    re.compile(r"\bfin\s+de\s+(?:mes|semana|trimestre|a[ñn]o)\b", re.IGNORECASE),
]


def _has_periphrastic_future(sent) -> bool:
    """``voy a + INF`` / ``vamos a + INF`` / ``va a + INF`` etc."""
    tokens = list(sent)
    for i, tok in enumerate(tokens[:-2]):
        if tok.lemma_ != "ir":
            continue
        if tok.pos_ not in ("VERB", "AUX"):
            continue
        # Need "a" immediately or one position after, then an infinitive
        for j in range(i + 1, min(i + 4, len(tokens))):
            if tokens[j].text.lower() == "a":
                # Look for an infinitive in the next 2 tokens
                for k in range(j + 1, min(j + 3, len(tokens))):
                    if "Inf" in tokens[k].morph.get("VerbForm"):
                        return True
                break
    return False


def _has_obligation(sent) -> bool:
    """``tener que + INF`` / ``hay que + INF``."""
    tokens = list(sent)
    for i, tok in enumerate(tokens[:-2]):
        if tok.lemma_ not in ("tener", "haber"):
            continue
        if tok.pos_ not in ("VERB", "AUX"):
            continue
        for j in range(i + 1, min(i + 4, len(tokens))):
            if tokens[j].text.lower() == "que":
                for k in range(j + 1, min(j + 4, len(tokens))):
                    if "Inf" in tokens[k].morph.get("VerbForm"):
                        return True
                break
    return False


def _has_intention(sent) -> bool:
    """``debo / necesito / pienso / quiero / pretendo + INF`` (nearby)."""
    tokens = list(sent)
    for i, tok in enumerate(tokens[:-1]):
        if tok.lemma_ not in ("deber", "necesitar", "pensar", "querer", "pretender"):
            continue
        if tok.pos_ not in ("VERB", "AUX"):
            continue
        # Infinitive within 4 tokens after
        for k in range(i + 1, min(i + 5, len(tokens))):
            if "Inf" in tokens[k].morph.get("VerbForm"):
                return True
    return False


def _has_imperative(sent) -> bool:
    for tok in sent:
        if tok.pos_ in ("VERB", "AUX") and "Imp" in tok.morph.get("Mood"):
            return True
    return False


def _has_simple_future(sent) -> bool:
    for tok in sent:
        if tok.pos_ in ("VERB", "AUX") and "Fut" in tok.morph.get("Tense"):
            return True
    return False


def _has_number(sent) -> bool:
    for tok in sent:
        if tok.like_num or tok.pos_ == "NUM":
            return True
    return False


def _has_date_word(text: str) -> bool:
    lower = text.lower()
    words = set(re.findall(r"\b[\w'áéíóúñü]+\b", lower))
    if words & _ES_DATE_WORDS:
        return True
    for pat in _ES_DATE_PATTERNS:
        if pat.search(text):
            return True
    # Bare ISO-style or numeric dates
    if re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", text):
        return True
    return False


def _has_ner(sent) -> bool:
    return len(sent.ents) > 0


def _has_question(text: str) -> bool:
    return "?" in text or "¿" in text


def _score_sentence(sent) -> int:
    text = sent.text
    score = 0
    if _has_ner(sent):
        score += 1
    if (
        _has_imperative(sent)
        or _has_simple_future(sent)
        or _has_periphrastic_future(sent)
        or _has_obligation(sent)
        or _has_intention(sent)
    ):
        score += 1
    if _has_number(sent) or _has_date_word(text):
        score += 1
    if _has_question(text):
        score += 1
    return score


def apply_c2(text: str) -> tuple[str, dict]:
    """Drop zero-signal non-edge sentences within each line."""
    nlp = _get_nlp_es()
    stats: dict = {
        "sentences_in": 0,
        "sentences_kept": 0,
        "sentences_dropped": 0,
        "dropped_examples": [],
    }
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue

        m = re.match(r"^(?P<speaker>[^:]{1,60}:\s*)(?P<body>.*)$", stripped)
        if m:
            head = m.group("speaker")
            body = m.group("body")
        else:
            head = ""
            body = stripped

        if not body.strip():
            out_lines.append(line)
            continue

        doc = nlp(body)
        sents = list(doc.sents)
        if not sents:
            out_lines.append(line)
            continue

        last_idx = len(sents) - 1
        kept_texts: list[str] = []
        for i, sent in enumerate(sents):
            stats["sentences_in"] += 1
            is_edge = i == 0 or i == last_idx
            score = _score_sentence(sent)
            if is_edge or score >= 1:
                kept_texts.append(sent.text.strip())
                stats["sentences_kept"] += 1
            else:
                stats["sentences_dropped"] += 1
                if len(stats["dropped_examples"]) < 30:
                    stats["dropped_examples"].append(sent.text.strip())

        new_body = " ".join(t for t in kept_texts if t)
        out_lines.append((head + new_body).rstrip())

    return "\n".join(out_lines), stats


# ---------------------------------------------------------------------------
# Gemini count_tokens helper
# ---------------------------------------------------------------------------


def _count_tokens(model: str, text: str) -> int:
    """Free, unmetered token count via Gemini's count_tokens API."""
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY (or GOOGLE_API_KEY) must be set in .env")
    client = genai.Client(api_key=api_key)
    result = client.models.count_tokens(model=model, contents=text)
    return int(result.total_tokens or 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _format_row(label: str, chars: int, tokens: int, base_tokens: int) -> str:
    if base_tokens == 0:
        pct = 0.0
    else:
        pct = ((base_tokens - tokens) / base_tokens) * 100
    return f"  {label:<32}  {chars:>7,} chars   {tokens:>6,} tokens   ({pct:+5.1f}%)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("page_id", help="Notion meeting page ID")
    parser.add_argument(
        "--model",
        default="gemini-3-flash-preview",
        help="Gemini model name for count_tokens (default matches prod)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("preview"),
        help="Directory for .{baseline,c1,c2,both}.txt outputs",
    )
    args = parser.parse_args()

    notion_token = os.environ.get("NOTION_API_TOKEN")
    if not notion_token:
        sys.exit("NOTION_API_TOKEN must be set in .env")
    client = NotionClientWrapper(
        NotionClient(auth=notion_token, notion_version="2026-03-11"),
    )

    print(f"Fetching transcript for page {args.page_id} ...", file=sys.stderr)
    raw, _attendees, metadata, _notes, _gov = fetch_transcript(args.page_id, client)
    if not raw.strip():
        sys.exit("No transcript text found on this page.")

    title = metadata.get("title", "(untitled)")
    print(f"  Meeting: {title}", file=sys.stderr)
    print(f"  Raw transcript: {len(raw):,} chars", file=sys.stderr)

    # Stage 1 — existing regex cleaner (Layer A+B). The current
    # production baseline: this is what reaches the LLM today.
    cleaned = run_cleaner(raw)
    baseline_text = cleaned.text
    print(
        f"  Existing cleaner: {cleaned.chars_before:,} -> "
        f"{cleaned.chars_after:,} chars ({cleaned.ratio:.0%} kept)",
        file=sys.stderr,
    )

    # Stage 2 — C1 alone (filler/discourse scrub on top of baseline).
    c1_text, c1_removed = apply_c1(baseline_text)
    print(
        f"  + C1 filler scrub: {len(baseline_text):,} -> "
        f"{len(c1_text):,} chars",
        file=sys.stderr,
    )

    # Stage 3 — C2 alone (signal-based sentence drop on top of baseline).
    print("  Loading spaCy es_core_news_sm …", file=sys.stderr)
    c2_text, c2_stats = apply_c2(baseline_text)
    print(
        f"  + C2 sentence drop: {len(baseline_text):,} -> "
        f"{len(c2_text):,} chars",
        file=sys.stderr,
    )

    # Stage 4 — both layers composed (C1 first, then C2).
    both_intermediate, _ = apply_c1(baseline_text)
    both_text, both_stats = apply_c2(both_intermediate)
    print(
        f"  + C1 -> C2:        {len(baseline_text):,} -> "
        f"{len(both_text):,} chars",
        file=sys.stderr,
    )

    # Stage 5 — count Gemini tokens for all four variants. Free, unmetered.
    print("Counting Gemini tokens (free count_tokens API) …", file=sys.stderr)
    tok_baseline = _count_tokens(args.model, baseline_text)
    tok_c1 = _count_tokens(args.model, c1_text)
    tok_c2 = _count_tokens(args.model, c2_text)
    tok_both = _count_tokens(args.model, both_text)

    # Stage 6 — write all four variants to disk for eyeballing.
    args.out_dir.mkdir(parents=True, exist_ok=True)
    pid = args.page_id
    baseline_path = args.out_dir / f"{pid}.baseline.txt"
    c1_path = args.out_dir / f"{pid}.c1.txt"
    c2_path = args.out_dir / f"{pid}.c2.txt"
    both_path = args.out_dir / f"{pid}.both.txt"
    baseline_path.write_text(baseline_text, encoding="utf-8")
    c1_path.write_text(c1_text, encoding="utf-8")
    c2_path.write_text(c2_text, encoding="utf-8")
    both_path.write_text(both_text, encoding="utf-8")

    # Stage 7 — report.
    print()
    print("=" * 80)
    print(f"  Meeting:     {title}")
    print(f"  Page ID:     {pid}")
    print("-" * 80)
    print(_format_row("baseline (current pipeline)", len(baseline_text), tok_baseline, tok_baseline))
    print(_format_row("c1   (filler scrub only)", len(c1_text), tok_c1, tok_baseline))
    print(_format_row("c2   (signal drop only)", len(c2_text), tok_c2, tok_baseline))
    print(_format_row("both (c1 -> c2)", len(both_text), tok_both, tok_baseline))
    print("-" * 80)
    print(f"  baseline: {baseline_path}")
    print(f"  c1:       {c1_path}")
    print(f"  c2:       {c2_path}")
    print(f"  both:     {both_path}")
    print("=" * 80)

    if c1_removed:
        print()
        print("Top 20 C1 removed tokens:")
        for tok, count in c1_removed.most_common(20):
            print(f"  {count:>4}  {tok}")
        total = sum(c1_removed.values())
        print(f"  -----")
        print(f"  {total:>4}  TOTAL C1 removals")

    print()
    sents_in = c2_stats["sentences_in"]
    sents_dropped = c2_stats["sentences_dropped"]
    drop_pct = (sents_dropped / max(sents_in, 1)) * 100
    print(
        f"C2 sentence drop: {sents_dropped:,} / {sents_in:,} sentences dropped "
        f"({drop_pct:.1f}%)"
    )
    examples = c2_stats["dropped_examples"]
    if examples:
        print()
        print(f"Sample of dropped sentences (first {len(examples)}):")
        for ex in examples:
            ex_short = ex if len(ex) <= 100 else ex[:97] + "..."
            print(f"  - {ex_short}")


if __name__ == "__main__":
    main()
