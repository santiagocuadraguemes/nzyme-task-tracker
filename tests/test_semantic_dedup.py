from unittest.mock import MagicMock

from src.semantic_dedup import SemanticDedup, cosine_similarity


def _make_embedding(values: list[float]) -> MagicMock:
    """Create a mock embedding data object."""
    obj = MagicMock()
    obj.embedding = values
    return obj


def _make_client(embeddings_by_call: list[list[list[float]]]) -> MagicMock:
    """Create a mock OpenAI client that returns embeddings in sequence.

    Each call to embeddings.create returns the next batch of embeddings.
    """
    client = MagicMock()
    responses = []
    for batch in embeddings_by_call:
        resp = MagicMock()
        resp.data = [_make_embedding(emb) for emb in batch]
        responses.append(resp)
    client.embeddings.create.side_effect = responses
    return client


class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert cosine_similarity([1, 0, 0], [1, 0, 0]) == 1.0

    def test_orthogonal_vectors(self):
        assert cosine_similarity([1, 0, 0], [0, 1, 0]) == 0.0

    def test_similar_vectors(self):
        score = cosine_similarity([1, 0.1, 0], [1, 0.2, 0])
        assert 0.99 < score < 1.0

    def test_zero_vector(self):
        assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0


class TestSemanticDedup:
    def test_detects_similar_title(self):
        # Batch 1: existing titles embedding
        # Call 2: new title embedding (similar to first existing)
        client = _make_client([
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],  # batch: "task A", "task B"
            [[0.99, 0.05, 0.0]],                     # single: "task A rephrased" (similar to A)
        ])
        dedup = SemanticDedup(client, ["task A", "task B"], threshold=0.85)

        is_dup, matched, score = dedup.is_duplicate("task A rephrased")

        assert is_dup is True
        assert matched == "task A"
        assert score > 0.85

    def test_allows_different_title(self):
        client = _make_client([
            [[1.0, 0.0, 0.0]],  # batch: "task A"
            [[0.0, 1.0, 0.0]],  # single: "completely different" (orthogonal)
        ])
        dedup = SemanticDedup(client, ["task A"], threshold=0.85)

        is_dup, matched, score = dedup.is_duplicate("completely different")

        assert is_dup is False
        assert matched is None
        assert score < 0.85

    def test_empty_existing_always_allows(self):
        client = _make_client([])
        dedup = SemanticDedup(client, [], threshold=0.85)

        is_dup, matched, score = dedup.is_duplicate("any task")

        # No embedding call needed — no existing titles to compare against
        assert is_dup is False
        assert score == 0.0

    def test_add_title_enables_within_batch_dedup(self):
        client = _make_client([
            [[1.0, 0.0, 0.0]],    # single: add_title("task A")
            [[0.98, 0.05, 0.0]],   # single: is_duplicate check for "task A variant")
        ])
        dedup = SemanticDedup(client, [], threshold=0.85)
        dedup.add_title("task A")

        is_dup, matched, score = dedup.is_duplicate("task A variant")

        assert is_dup is True
        assert matched == "task A"

    def test_threshold_boundary(self):
        # Score exactly at threshold
        client = _make_client([
            [[1.0, 0.0, 0.0]],  # batch: existing
            [[0.85, 0.53, 0.0]],  # single: check (cosine ~0.85)
        ])
        dedup = SemanticDedup(client, ["existing"], threshold=0.85)

        is_dup, _, score = dedup.is_duplicate("borderline")

        # cosine_similarity([1,0,0], [0.85,0.53,0]) ≈ 0.848... which is < 0.85
        assert is_dup is False

    def test_existing_count(self):
        client = _make_client([
            [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        ])
        dedup = SemanticDedup(client, ["a", "b", "c"])
        assert dedup.existing_count == 3

    def test_graceful_on_api_error(self):
        """If embeddings API fails during init, the exception propagates.

        The pipeline catches this in _load_sync_context and sets
        semantic_dedup to None, so the dedup step is skipped.
        """
        client = MagicMock()
        client.embeddings.create.side_effect = Exception("API unavailable")

        try:
            SemanticDedup(client, ["task A"])
            assert False, "Should have raised"
        except Exception as e:
            assert "API unavailable" in str(e)
