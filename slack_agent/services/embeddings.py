"""
Lazy-loaded sentence-transformers embedding service.

Used for semantic memory recall: instead of LIKE '%keyword%', we compute
cosine similarity between the query embedding and stored memory embeddings.

Model: all-MiniLM-L6-v2 (384 dims, ~80 MB, already bundled in Docker image).
Falls back to keyword search transparently if model is unavailable.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"
_MODEL_PATH = os.getenv("EMBEDDING_MODEL_PATH", "/app/models/all-MiniLM-L6-v2")

_model = None
_load_attempted = False


def _get_model():
    global _model, _load_attempted
    if _load_attempted:
        return _model
    _load_attempted = True
    try:
        from sentence_transformers import SentenceTransformer
        path = _MODEL_PATH if os.path.isdir(_MODEL_PATH) else _MODEL_NAME
        _model = SentenceTransformer(path)
        logger.info("Embedding model loaded from '%s'", path)
    except Exception as exc:
        logger.warning("Embedding model unavailable — semantic search disabled: %s", exc)
        _model = None
    return _model


def is_available() -> bool:
    return _get_model() is not None


def embed(text: str) -> Optional[bytes]:
    """
    Generate a normalized float32 embedding for *text*.
    Returns raw bytes (numpy float32 array) or None if model unavailable.
    """
    model = _get_model()
    if model is None:
        return None
    try:
        import numpy as np
        vec = model.encode(text, normalize_embeddings=True)
        return vec.astype(np.float32).tobytes()
    except Exception as exc:
        logger.warning("embed() failed: %s", exc)
        return None


def cosine(a_bytes: bytes, b_bytes: bytes) -> float:
    """
    Cosine similarity between two float32 embedding byte strings.
    Since embeddings are L2-normalized, dot product == cosine similarity.
    """
    import numpy as np
    a = np.frombuffer(a_bytes, dtype=np.float32)
    b = np.frombuffer(b_bytes, dtype=np.float32)
    return float(np.dot(a, b))


def rank_by_similarity(
    query_bytes: bytes,
    candidates: list[dict],
    embedding_key: str = "embedding",
    top_k: int = 10,
) -> list[dict]:
    """
    Rank *candidates* by cosine similarity to *query_bytes*.

    Each candidate is a dict. Those without an embedding (None / empty)
    receive score 0.0 and are placed at the end.

    Returns top_k candidates sorted by descending similarity.
    """
    scored: list[tuple[float, dict]] = []
    for row in candidates:
        emb = row.get(embedding_key)
        score = cosine(query_bytes, emb) if emb else 0.0
        scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:top_k]]
