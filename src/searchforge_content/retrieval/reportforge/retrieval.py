from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence

from .corpus import chunk_documents, load_documents
from .models import Chunk, Document, SearchHit

TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u3400-\u9fff]")
FIXTURE_DIM = 256


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(text.lower()):
        if len(raw) == 1 and "\u3400" <= raw <= "\u9fff":
            tokens.append(raw)
        else:
            tokens.append(raw)
    # Add Chinese bigrams so phrases can match without a downloaded tokenizer.
    for run in re.findall(r"[\u3400-\u9fff]{2,}", text):
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def fixture_vector(text: str, dimensions: int = FIXTURE_DIM) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        vector[value % dimensions] += 1.0 if value & (1 << 63) else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> dict[str, float]:
    if k <= 0:
        raise ValueError("RRF k must be positive")
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights length must match rankings length")
    scores: dict[str, float] = defaultdict(float)
    for ranking, weight in zip(rankings, weights):
        if weight < 0:
            raise ValueError("RRF weights must be nonnegative")
        seen: set[str] = set()
        for rank, item_id in enumerate(ranking, 1):
            if item_id in seen:
                continue
            seen.add(item_id)
            scores[item_id] += float(weight) / (k + rank)
    return dict(scores)


def _bm25_scores(query: str, chunks: Sequence[Chunk], k1: float = 1.5, b: float = 0.75) -> list[float]:
    query_terms = Counter(tokenize(query))
    tokenized = [tokenize(chunk.text) for chunk in chunks]
    lengths = [len(tokens) for tokens in tokenized]
    avg_len = (sum(lengths) / len(lengths)) if lengths else 0.0
    if not avg_len:
        return [0.0] * len(chunks)
    doc_freq: Counter[str] = Counter()
    frequencies: list[Counter[str]] = []
    for tokens in tokenized:
        tf = Counter(tokens)
        frequencies.append(tf)
        doc_freq.update(tf.keys())
    count = len(chunks)
    scores: list[float] = []
    for tf, length in zip(frequencies, lengths):
        score = 0.0
        for term, qtf in query_terms.items():
            freq = tf.get(term, 0)
            if not freq:
                continue
            idf = math.log(1 + (count - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            denom = freq + k1 * (1 - b + b * length / avg_len)
            score += idf * (freq * (k1 + 1) / denom) * min(qtf, 2)
        scores.append(score)
    return scores


def _write_jsonl(path: Path, records: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _corpus_fingerprint(documents: Sequence[Document]) -> str:
    """Hash document identities and complete normalized contents unambiguously."""
    digest = hashlib.sha256()
    for document in documents:
        doc_id = document.doc_id.encode("utf-8")
        text = document.text.encode("utf-8")
        digest.update(len(doc_id).to_bytes(8, "big"))
        digest.update(doc_id)
        digest.update(len(text).to_bytes(8, "big"))
        digest.update(text)
    return digest.hexdigest()


def build_index(
    corpus_path: Path,
    output_dir: Path,
    *,
    backend: str,
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B",
    document_prompt_name: str = "document",
    query_prompt_name: str = "query",
    chunk_chars: int = 1800,
    overlap_chars: int = 180,
) -> dict:
    if backend not in {"fixture", "faiss"}:
        raise ValueError("backend must be 'fixture' or 'faiss'")
    output_dir = output_dir.resolve()
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise FileExistsError(f"Refusing to replace nonempty index directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = load_documents(corpus_path)
    chunks = chunk_documents(documents, chunk_chars, overlap_chars)
    chunks_path = output_dir / "chunks.jsonl"
    _write_jsonl(chunks_path, (chunk.to_dict() for chunk in chunks))

    corpus_fingerprint = _corpus_fingerprint(documents)
    manifest = {
        "schema_version": 2,
        "backend": backend,
        "embedding_model": embedding_model if backend == "faiss" else None,
        "document_prompt_name": document_prompt_name if backend == "faiss" else None,
        "query_prompt_name": query_prompt_name if backend == "faiss" else None,
        "vector_metric": "inner_product_on_l2_normalized_vectors",
        "chunk_count": len(chunks),
        "document_count": len(documents),
        "chunk_chars": chunk_chars,
        "overlap_chars": overlap_chars,
        "corpus_fingerprint": corpus_fingerprint,
        "chunks_sha256": _sha256_file(chunks_path),
        "fixture_backend_notice": (
            "Explicit deterministic hashed vectors for local smoke/tests; not a learned embedding model."
            if backend == "fixture" else None
        ),
    }
    if backend == "fixture":
        vectors = [fixture_vector(chunk.text) for chunk in chunks]
        vectors_path = output_dir / "dense_fixture.jsonl"
        _write_jsonl(vectors_path, ({"vector": vector} for vector in vectors))
        manifest["dense_vector_count"] = len(vectors)
        manifest["embedding_dimension"] = FIXTURE_DIM
        manifest["dense_vectors_sha256"] = _sha256_file(vectors_path)
    else:
        try:
            import faiss
            import numpy as np
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "FAISS indexing requires the optional retrieval dependencies. Install with "
                "`python -m pip install -e '.[retrieval]'`; no fixture fallback was used."
            ) from exc
        model = SentenceTransformer(embedding_model)
        encode_kwargs = {}
        if document_prompt_name:
            encode_kwargs["prompt_name"] = document_prompt_name
        vectors = model.encode(
            [chunk.text for chunk in chunks],
            batch_size=32,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=True,
            **encode_kwargs,
        ).astype("float32")
        if vectors.ndim != 2 or vectors.shape[0] != len(chunks):
            raise RuntimeError("Embedding model returned an invalid vector matrix")
        index = faiss.IndexFlatIP(int(vectors.shape[1]))
        index.add(vectors)
        faiss.write_index(index, str(output_dir / "dense.faiss"))
        manifest["dense_vector_count"] = int(index.ntotal)
        manifest["embedding_dimension"] = int(vectors.shape[1])
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


class Retriever:
    def __init__(self, index_dir: Path, *, reranker: str = "none", reranker_model: str | None = None):
        self.index_dir = index_dir.resolve()
        manifest_path = self.index_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Index manifest not found: {manifest_path}")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("schema_version") != 2:
            raise ValueError("Unsupported index manifest schema; rebuild this index with the current CLI")
        self.backend = self.manifest["backend"]
        if self.backend not in {"fixture", "faiss"}:
            raise ValueError(f"Unknown index backend: {self.backend}")
        with (self.index_dir / "chunks.jsonl").open("r", encoding="utf-8") as handle:
            self.chunks = [Chunk.from_dict(json.loads(line)) for line in handle if line.strip()]
        if not self.chunks:
            raise ValueError("Index contains no chunks")
        if self.manifest.get("chunk_count") != len(self.chunks):
            raise ValueError("Index manifest chunk_count does not match chunks.jsonl")
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("Index contains duplicate chunk IDs")
        chunks_path = self.index_dir / "chunks.jsonl"
        if self.manifest.get("chunks_sha256") != _sha256_file(chunks_path):
            raise ValueError("chunks.jsonl SHA-256 does not match the index manifest")
        self.reranker_mode = reranker
        self.reranker_model = reranker_model
        if reranker not in {"none", "fixture", "cross-encoder"}:
            raise ValueError("reranker must be none, fixture, or cross-encoder")
        if reranker == "fixture" and self.backend != "fixture":
            raise ValueError("fixture reranking is only allowed with an explicit fixture index")
        self._fixture_vectors: list[list[float]] | None = None
        self._faiss = None
        self._embedder = None
        self._cross_encoder = None
        if self.backend == "fixture":
            vectors_path = self.index_dir / "dense_fixture.jsonl"
            if self.manifest.get("dense_vectors_sha256") != _sha256_file(vectors_path):
                raise ValueError("dense_fixture.jsonl SHA-256 does not match the index manifest")
            with vectors_path.open("r", encoding="utf-8") as handle:
                self._fixture_vectors = [json.loads(line)["vector"] for line in handle if line.strip()]
            dimension = self.manifest.get("embedding_dimension")
            if self.manifest.get("dense_vector_count") != len(self._fixture_vectors):
                raise ValueError("Index manifest dense_vector_count does not match fixture vectors")
            if len(self._fixture_vectors) != len(self.chunks):
                raise ValueError("Fixture vector count does not match chunks.jsonl")
            if not isinstance(dimension, int) or dimension <= 0:
                raise ValueError("Index manifest embedding_dimension must be a positive integer")
            for row_number, vector in enumerate(self._fixture_vectors, 1):
                if not isinstance(vector, list) or len(vector) != dimension:
                    raise ValueError(f"Invalid fixture vector dimensions at row {row_number}")
                if any(not math.isfinite(float(value)) for value in vector):
                    raise ValueError(f"Non-finite fixture vector value at row {row_number}")
        else:
            try:
                import faiss
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "This index uses FAISS/SentenceTransformers. Install `.[retrieval]`; "
                    "no fixture fallback was used."
                ) from exc
            self._faiss = faiss.read_index(str(self.index_dir / "dense.faiss"))
            if self.manifest.get("dense_vector_count") != self._faiss.ntotal:
                raise ValueError("Index manifest dense_vector_count does not match dense.faiss")
            if self.manifest.get("embedding_dimension") != self._faiss.d:
                raise ValueError("Index manifest embedding_dimension does not match dense.faiss")
            if self._faiss.ntotal != len(self.chunks):
                raise ValueError("FAISS vector count does not match chunks.jsonl")
            self._embedder = SentenceTransformer(self.manifest["embedding_model"])
        if reranker == "cross-encoder":
            if not reranker_model:
                raise ValueError("--reranker-model is required for cross-encoder reranking")
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RuntimeError("CrossEncoder requires sentence-transformers; no fallback was used") from exc
            self._cross_encoder = CrossEncoder(reranker_model)

    def _dense_scores(self, query: str) -> list[float]:
        if self.backend == "fixture":
            assert self._fixture_vectors is not None
            qvec = fixture_vector(query)
            return [sum(a * b for a, b in zip(qvec, vector)) for vector in self._fixture_vectors]
        assert self._embedder is not None and self._faiss is not None
        import numpy as np

        encode_kwargs = {}
        prompt_name = self.manifest.get("query_prompt_name")
        if prompt_name:
            encode_kwargs["prompt_name"] = prompt_name
        qvec = self._embedder.encode(
            [query], convert_to_numpy=True, normalize_embeddings=True, **encode_kwargs
        ).astype("float32")
        distances, indices = self._faiss.search(qvec, len(self.chunks))
        scores = [float("-inf")] * len(self.chunks)
        for score, idx in zip(distances[0], indices[0]):
            if idx >= 0:
                scores[int(idx)] = float(score)
        return scores

    @staticmethod
    def _ranking(scores: Sequence[float], chunks: Sequence[Chunk]) -> list[int]:
        return sorted(range(len(chunks)), key=lambda idx: (-scores[idx], chunks[idx].chunk_id))

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        method: str = "hybrid",
        reranker: str | None = None,
        pool_k: int | None = None,
    ) -> list[SearchHit]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if method not in {"dense", "bm25", "hybrid", "hybrid-rerank"}:
            raise ValueError("method must be dense, bm25, hybrid, or hybrid-rerank")
        rerank_mode = reranker or self.reranker_mode
        if method == "hybrid-rerank" and rerank_mode == "none":
            raise ValueError("hybrid-rerank requires explicit --reranker fixture or cross-encoder")

        dense_scores = self._dense_scores(query) if method != "bm25" else None
        bm25_scores = _bm25_scores(query, self.chunks) if method != "dense" else None
        dense_order = self._ranking(dense_scores, self.chunks) if dense_scores is not None else []
        bm25_order = self._ranking(bm25_scores, self.chunks) if bm25_scores is not None else []
        dense_rank = {self.chunks[idx].chunk_id: rank for rank, idx in enumerate(dense_order, 1)}
        bm25_rank = {self.chunks[idx].chunk_id: rank for rank, idx in enumerate(bm25_order, 1)}
        if method == "dense":
            selected = dense_order
            rrf_scores: dict[str, float] = {}
        elif method == "bm25":
            selected = bm25_order
            rrf_scores = {}
        else:
            rankings = [[self.chunks[idx].chunk_id for idx in dense_order], [self.chunks[idx].chunk_id for idx in bm25_order]]
            rrf_scores = reciprocal_rank_fusion(rankings)
            selected = sorted(
                range(len(self.chunks)),
                key=lambda idx: (-rrf_scores.get(self.chunks[idx].chunk_id, 0.0), self.chunks[idx].chunk_id),
            )

        limit = pool_k or max(top_k * 4, top_k)
        candidates = selected[:limit]
        rerank_scores: dict[int, float] = {}
        if method == "hybrid-rerank":
            if rerank_mode == "fixture":
                qterms = set(tokenize(query))
                for idx in candidates:
                    cterms = set(tokenize(self.chunks[idx].text))
                    rerank_scores[idx] = len(qterms & cterms) / max(1, len(qterms))
            elif rerank_mode == "cross-encoder":
                if self._cross_encoder is None:
                    raise ValueError("CrossEncoder was not initialized; create Retriever with a model")
                pairs = [(query, self.chunks[idx].text) for idx in candidates]
                values = self._cross_encoder.predict(pairs)
                rerank_scores = {idx: float(value) for idx, value in zip(candidates, values)}
            else:
                raise ValueError(f"Unknown reranker mode: {rerank_mode}")
            candidates.sort(key=lambda idx: (-rerank_scores[idx], self.chunks[idx].chunk_id))

        hits: list[SearchHit] = []
        for rank, idx in enumerate(candidates[:top_k], 1):
            chunk = self.chunks[idx]
            dense_score = dense_scores[idx] if dense_scores is not None else None
            bm25_score = bm25_scores[idx] if bm25_scores is not None else None
            rrf_score = rrf_scores.get(chunk.chunk_id) if rrf_scores else None
            rerank_score = rerank_scores.get(idx)
            score = rerank_score if rerank_score is not None else rrf_score if rrf_score is not None else dense_score if dense_score is not None else bm25_score
            hits.append(
                SearchHit(
                    chunk=chunk,
                    score=float(score or 0.0),
                    dense_score=float(dense_score) if dense_score is not None else None,
                    bm25_score=float(bm25_score) if bm25_score is not None else None,
                    rrf_score=float(rrf_score) if rrf_score is not None else None,
                    rerank_score=float(rerank_score) if rerank_score is not None else None,
                    dense_rank=dense_rank.get(chunk.chunk_id),
                    bm25_rank=bm25_rank.get(chunk.chunk_id),
                    rerank_rank=rank if rerank_score is not None else None,
                )
            )
        return hits
