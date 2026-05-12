"""Unit tests for transcript_cleaner.

The cleaner is deterministic, so tests pin the exact before/after shape.
"""

from __future__ import annotations

from src.transcript_pipeline.transcript_cleaner import clean


class TestLayerA:
    def test_strips_pure_timestamp_lines(self):
        text = "Santiago: hello\n[00:01:23]\n00:02:45\n00:03:00 --> 00:03:10\nJacob: hi"
        result = clean(text)
        assert "00:01:23" not in result.text
        assert "00:02:45" not in result.text
        assert "00:03:00" not in result.text
        assert "Santiago: hello" in result.text
        assert "Jacob: hi" in result.text

    def test_drops_bare_speaker_labels(self):
        text = "Santiago:\nSpeaker 2:\nJacob: actual content"
        result = clean(text)
        assert "Santiago:" not in result.text
        assert "Speaker 2:" not in result.text
        assert "Jacob: actual content" in result.text

    def test_collapses_blank_line_runs(self):
        text = "Santiago: hi\n\n\n\nJacob: hello"
        result = clean(text)
        assert "\n\n\n" not in result.text

    def test_trims_per_line_whitespace(self):
        text = "   Santiago: hello   \n   Jacob: hi   "
        result = clean(text)
        assert result.text == "Santiago: hello\nJacob: hi"

    def test_empty_transcript_no_op(self):
        assert clean("").text == ""

    def test_no_speaker_prefix_passes_through(self):
        # Transcripts without a "Name:" format aren't speaker-collapsed,
        # but lines must not be dropped.
        text = "this is just narrative text\nwithout any speaker labels"
        result = clean(text)
        assert "narrative text" in result.text
        assert "speaker labels" in result.text


class TestLayerB:
    def test_collapses_consecutive_same_speaker(self):
        text = (
            "Santiago: yeah\n"
            "Santiago: yeah it was\n"
            "Santiago: yeah it was good\n"
            "Jacob: nice"
        )
        result = clean(text)
        # Two output utterances total — one Santiago, one Jacob.
        assert result.text.count("Santiago:") == 1
        assert result.text.count("Jacob:") == 1
        assert "yeah it was good" in result.text

    def test_does_not_merge_different_speakers(self):
        text = "Santiago: hi\nJacob: hello\nSantiago: bye"
        result = clean(text)
        # Three distinct utterances preserved in order.
        lines = result.text.splitlines()
        assert lines == ["Santiago: hi", "Jacob: hello", "Santiago: bye"]

    def test_drops_adjacent_identical_sentences_within_utterance(self):
        text = "Santiago: yes. yes. We should do it."
        result = clean(text)
        # "yes." appears once even though it was doubled.
        assert result.text.lower().count("yes.") == 1
        assert "We should do it" in result.text

    def test_continuation_line_attaches_to_pending_speaker(self):
        # A line without a speaker prefix following a speaker line is
        # treated as continuation of that speaker.
        text = "Santiago: starting a thought\nthat continues here\nJacob: ok"
        result = clean(text)
        lines = result.text.splitlines()
        assert lines[0] == "Santiago: starting a thought that continues here"
        assert lines[1] == "Jacob: ok"

    def test_preserves_speaker_n_format(self):
        text = "Speaker 1: hello\nSpeaker 2: hi\nSpeaker 1: bye"
        result = clean(text)
        lines = result.text.splitlines()
        assert lines == ["Speaker 1: hello", "Speaker 2: hi", "Speaker 1: bye"]


class TestMetrics:
    def test_reports_before_and_after_sizes(self):
        text = "Santiago:\n\n\nJacob: hello"
        result = clean(text)
        assert result.chars_before == len(text)
        assert result.chars_after == len(result.text)
        # Cleanup shrinks the transcript here.
        assert result.chars_after < result.chars_before
        assert 0.0 < result.ratio < 1.0

    def test_empty_input_zero_metrics(self):
        result = clean("")
        assert result.chars_before == 0
        assert result.chars_after == 0
        assert result.ratio == 0.0


class TestSafety:
    def test_does_not_clip_content_lines_with_colons(self):
        # A content line that ends with ':' must not be treated as a
        # bare speaker label and dropped.
        text = "Santiago: the agenda is the following:\n- first item"
        result = clean(text)
        assert "the agenda is the following" in result.text
        assert "first item" in result.text

    def test_does_not_match_lowercase_pseudo_label(self):
        # "but I think:" should not be treated as a speaker label.
        text = "Santiago: but I think: we should proceed."
        result = clean(text)
        assert "but I think" in result.text
        assert "we should proceed" in result.text
