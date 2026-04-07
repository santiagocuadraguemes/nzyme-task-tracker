"""Embedding-based semantic deduplication for extracted tasks."""
from __future__ import annotations

import logging

from openai import OpenAI

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
BATCH_SIZE = 2048  # OpenAI embeddings API max batch size


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticDedup:
    """Detects duplicate tasks using embedding cosine similarity.

    Compares new task titles against existing titles. Two titles with
    similarity above the threshold are considered duplicates, even if
    worded differently or in different languages.
    """

    def __init__(
        self,
        openai_client: OpenAI,
        existing_titles: list[str],
        threshold: float = 0.85,
    ) -> None:
        self._client = openai_client
        self._threshold = threshold
        self._existing: list[tuple[str, list[float]]] = []
        self._embed_existing(existing_titles)

    def _embed_existing(self, titles: list[str]) -> None:
        """Compute embeddings for all existing titles in batches."""
        if not titles:
            return
        for i in range(0, len(titles), BATCH_SIZE):
            batch = titles[i : i + BATCH_SIZE]
            response = self._client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=batch,
            )
            for title, data in zip(batch, response.data):
                self._existing.append((title, data.embedding))

    def _embed_one(self, text: str) -> list[float]:
        """Compute embedding for a single title."""
        response = self._client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[text],
        )
        return response.data[0].embedding

    def is_duplicate(self, title: str) -> tuple[bool, str | None, float]:
        """Check if title is semantically similar to any existing title.

        Returns (is_duplicate, matched_title, similarity_score).
        """
        if not self._existing:
            return False, None, 0.0

        new_emb = self._embed_one(title)
        best_score = 0.0
        best_match: str | None = None

        for existing_title, existing_emb in self._existing:
            score = cosine_similarity(new_emb, existing_emb)
            if score > best_score:
                best_score = score
                best_match = existing_title

        if best_score >= self._threshold:
            return True, best_match, best_score
        return False, None, best_score

    def add_title(self, title: str) -> None:
        """Add a newly created task to the existing set (within-batch dedup)."""
        emb = self._embed_one(title)
        self._existing.append((title, emb))

    @property
    def existing_count(self) -> int:
        return len(self._existing)
