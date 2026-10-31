"""Overflow-safe CrossEncoder ranking for query/document text pairs.

Pairs that fit are passed to ``reranker.predict`` with their original text,
unchanged. If an original pair exceeds the configured token limit, both sides
are partitioned on the pinned tokenizer's offsets. The available content-token
budget is first split in proportion to the two source lengths; the exact paired
encoding is then checked and the larger side's window budget is reduced until
every Cartesian-product window pair fits. The requested overlap is clamped to
at most ``window_tokens - 1`` so windows always advance.

All source characters and tokenizer tokens must be covered by the windows.
There is no expansion cap: if the tokenizer cannot produce a valid full-coverage
plan, the helper raises with the original pair identity instead of dropping or
truncating a candidate.
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np


def _as_flat_list(values: Any, field: str) -> list[Any]:
    """Flatten a single-string tokenizer result and reject accidental batches."""
    if hasattr(values, "tolist"):
        values = values.tolist()
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"tokenizer {field} is not a list")
    values = list(values)
    if values and isinstance(values[0], (list, tuple)):
        # A single string's offset mapping is itself a list of [start, end]
        # pairs; input_ids, by contrast, should only be nested for a batch.
        is_offset_pair = (
            field == "offset_mapping"
            and len(values[0]) == 2
            and not isinstance(values[0][0], (list, tuple))
        )
        if not is_offset_pair:
            if len(values) != 1:
                raise ValueError(f"tokenizer unexpectedly returned a batch for one string ({field})")
            values = list(values[0])
    return values


def _pair_lengths(tokenizer: Any, pairs: Sequence[tuple[str, str]],
                  batch_size: int) -> list[int]:
    """Return exact pair lengths including special tokens, without truncation."""
    lengths: list[int] = []
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start:start + batch_size]
        encoded = tokenizer(
            [pair[0] for pair in batch],
            [pair[1] for pair in batch],
            add_special_tokens=True,
            truncation=False,
            padding=False,
        )
        input_ids = encoded["input_ids"]
        if hasattr(input_ids, "tolist"):
            input_ids = input_ids.tolist()
        if not isinstance(input_ids, (list, tuple)) or len(input_ids) != len(batch):
            raise ValueError("tokenizer returned malformed pair input_ids")
        for row in input_ids:
            if hasattr(row, "tolist"):
                row = row.tolist()
            lengths.append(len(row))
    return lengths


def _source_offsets(tokenizer: Any, text: str) -> list[tuple[int, int]]:
    """Tokenize source text with offsets; slow tokenizers cannot qualify safely."""
    try:
        encoded = tokenizer(
            text,
            add_special_tokens=False,
            truncation=False,
            padding=False,
            return_offsets_mapping=True,
        )
    except (TypeError, NotImplementedError, ValueError) as exc:
        raise ValueError(
            "overflow windowing requires tokenizer offset_mapping on the original text"
        ) from exc
    if "offset_mapping" not in encoded or "input_ids" not in encoded:
        raise ValueError("tokenizer did not return input_ids and offset_mapping")
    input_ids = _as_flat_list(encoded["input_ids"], "input_ids")
    raw_offsets = _as_flat_list(encoded["offset_mapping"], "offset_mapping")
    if len(input_ids) != len(raw_offsets):
        raise ValueError("tokenizer offset count does not match token count")
    offsets: list[tuple[int, int]] = []
    prior_start = -1
    for raw in raw_offsets:
        if hasattr(raw, "tolist"):
            raw = raw.tolist()
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError("tokenizer returned a malformed character offset")
        start, end = int(raw[0]), int(raw[1])
        if not (0 <= start < end <= len(text)):
            raise ValueError(
                "tokenizer offset does not map a token to source characters; refusing lossy windowing"
            )
        if start < prior_start:
            raise ValueError("tokenizer character offsets are not monotonic")
        offsets.append((start, end))
        prior_start = start
    if not offsets and text.strip():
        raise ValueError("tokenizer produced no source offsets for non-whitespace text")
    return offsets


def _window_ranges(token_count: int, cap: int, overlap: int) -> list[tuple[int, int]]:
    if token_count == 0:
        return [(0, 0)]
    if cap < 1:
        raise ValueError("window token budget must be positive")
    effective_overlap = min(overlap, cap - 1)
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < token_count:
        end = min(token_count, start + cap)
        ranges.append((start, end))
        if end == token_count:
            break
        next_start = end - effective_overlap
        if next_start <= start:
            raise ValueError("windowing failed to advance")
        start = next_start
    return ranges


def _union_character_coverage(ranges: Sequence[tuple[int, int]]) -> int:
    if not ranges:
        return 0
    ordered = sorted(ranges)
    covered = 0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            covered += end - start
            start, end = next_start, next_end
    return covered + end - start


def _make_windows(text: str, offsets: Sequence[tuple[int, int]], cap: int,
                  overlap: int) -> tuple[list[dict[str, Any]], int, int]:
    """Build overlapping raw-text windows and verify token/character coverage."""
    token_count = len(offsets)
    ranges = _window_ranges(token_count, cap, overlap)
    windows: list[dict[str, Any]] = []
    covered_tokens: set[int] = set()
    char_ranges: list[tuple[int, int]] = []
    for token_start, token_end in ranges:
        if token_count == 0:
            char_start, char_end = 0, len(text)
        else:
            char_start = 0 if token_start == 0 else offsets[token_start][0]
            # Assign inter-token characters to the preceding window. The first
            # and last windows also retain leading and trailing source text.
            char_end = len(text) if token_end == token_count else offsets[token_end][0]
        if char_end < char_start:
            raise ValueError("tokenizer offsets produced a reversed source window")
        windows.append({
            "text": text[char_start:char_end],
            "token_start": token_start,
            "token_end": token_end,
            "char_start": char_start,
            "char_end": char_end,
        })
        covered_tokens.update(range(token_start, token_end))
        char_ranges.append((char_start, char_end))
    char_covered = _union_character_coverage(char_ranges)
    if len(covered_tokens) != token_count or char_covered != len(text):
        raise ValueError(
            f"window coverage failed (tokens {len(covered_tokens)}/{token_count}, "
            f"characters {char_covered}/{len(text)})"
        )
    return windows, token_count, char_covered


def _product_batches(query_windows: Sequence[dict[str, Any]],
                     document_windows: Sequence[dict[str, Any]],
                     batch_size: int):
    batch: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pair in itertools.product(query_windows, document_windows):
        batch.append(pair)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _fit_overflow_windows(tokenizer: Any, query_text: str, document_text: str,
                          query_offsets: Sequence[tuple[int, int]],
                          document_offsets: Sequence[tuple[int, int]], *,
                          batch_size: int, limit: int,
                          overlap: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Find proportional side budgets whose full window product fits exactly."""
    query_count, document_count = len(query_offsets), len(document_offsets)
    empty_length = _pair_lengths(tokenizer, [("", "")], batch_size)[0]
    content_budget = limit - empty_length
    min_query = 1 if query_count else 0
    min_document = 1 if document_count else 0
    if content_budget < min_query + min_document:
        raise ValueError(
            "token limit cannot fit one token from each nonempty side plus pair special tokens"
        )

    if query_count == 0:
        query_cap, document_cap = 0, min(document_count, content_budget)
    elif document_count == 0:
        query_cap, document_cap = min(query_count, content_budget), 0
    else:
        query_cap = round(content_budget * query_count / (query_count + document_count))
        query_cap = min(content_budget - 1, max(1, query_cap))
        query_cap = min(query_count, query_cap)
        document_cap = min(document_count, content_budget - query_cap)
        if document_cap < 1:
            document_cap = 1
            query_cap = min(query_count, content_budget - 1)

    while True:
        query_windows, _, _ = _make_windows(query_text, query_offsets, query_cap, overlap)
        document_windows, _, _ = _make_windows(document_text, document_offsets,
                                               document_cap, overlap)
        violation: tuple[dict[str, Any], dict[str, Any], int, int, int] | None = None
        maximum = 0
        for batch in _product_batches(query_windows, document_windows, batch_size):
            lengths = _pair_lengths(
                tokenizer,
                [(query_window["text"], document_window["text"])
                 for query_window, document_window in batch],
                batch_size,
            )
            maximum = max(maximum, max(lengths, default=0))
            for (query_window, document_window), pair_length in zip(batch, lengths):
                if pair_length > limit:
                    q_len, d_len = _pair_lengths(
                        tokenizer,
                        [(query_window["text"], ""), ("", document_window["text"])],
                        batch_size,
                    )
                    violation = (query_window, document_window, pair_length, q_len, d_len)
                    break
            if violation:
                break
        if violation is None:
            return query_windows, document_windows, maximum

        _, _, pair_length, query_piece_tokens, document_piece_tokens = violation
        reduce_query = query_cap > min_query and (
            document_cap <= min_document or query_piece_tokens >= document_piece_tokens
        )
        reduce_document = document_cap > min_document and not reduce_query
        if not reduce_query and not reduce_document:
            raise ValueError(
                f"even the smallest full-coverage subwindow pair has {pair_length} tokens "
                f"(limit {limit})"
            )
        excess = max(1, pair_length - limit)
        if reduce_query:
            query_cap = max(min_query, query_cap - excess)
        else:
            document_cap = max(min_document, document_cap - excess)


def _predict(reranker: Any, pairs: Sequence[tuple[str, str]], batch_size: int) -> list[float]:
    if not pairs:
        return []
    values = np.asarray(reranker.predict(
        list(pairs), batch_size=batch_size, show_progress_bar=False
    ), dtype=np.float64)
    if values.ndim == 2 and values.shape[1] == 1:
        values = values[:, 0]
    if values.ndim != 1 or len(values) != len(pairs) or not np.isfinite(values).all():
        raise ValueError("CrossEncoder returned malformed or nonfinite scores")
    return [float(value) for value in values.tolist()]


def _source_cached(cache: dict[Any, tuple[str, list[tuple[int, int]]]],
                   key: Any, tokenizer: Any, text: str) -> list[tuple[int, int]]:
    cached = cache.get(key)
    if cached is not None:
        if cached[0] != text:
            raise ValueError("source cache key was reused for different text")
        return cached[1]
    offsets = _source_offsets(tokenizer, text)
    cache[key] = (text, offsets)
    return offsets


def rank_with_windowing(reranker: Any,
                        segments: Sequence[Mapping[str, Any]],
                        candidates: Sequence[Mapping[str, Any]],
                        load_bodies: Callable[[list[int]], Mapping[int, str]], *,
                        batch_size: int = 16,
                        limit: int = 512,
                        overlap: int = 32
                        ) -> tuple[list[Mapping[str, Any]], dict[str, float], float, dict[str, Any]]:
    """Score every segment/candidate pair, windowing only over-limit pairs.

    Per candidate, the score is the maximum across all query segments and any
    overflow subwindow pairs. Final candidate order is ``(-score, doc_id)``,
    matching the existing full-mix CrossEncoder aggregation rule.
    """
    for name, value in (("batch_size", batch_size), ("limit", limit)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if not isinstance(overlap, int) or isinstance(overlap, bool) or overlap < 0:
        raise ValueError("overlap must be a nonnegative integer")

    stats: dict[str, Any] = {
        "originalpairs": len(segments) * len(candidates),
        "expandedpairs": 0,
        "overflowpairs": 0,
        "max_input_tokens": 0,
        "max_original_input_tokens": 0,
        "query_windows": 0,
        "document_windows": 0,
        "coverage": {
            "complete": True,
            "query_tokens_total": 0,
            "query_tokens_covered": 0,
            "document_tokens_total": 0,
            "document_tokens_covered": 0,
            "query_characters_total": 0,
            "query_characters_covered": 0,
            "document_characters_total": 0,
            "document_characters_covered": 0,
        },
    }
    if not candidates:
        return [], {}, 0.0, stats
    if not segments:
        raise ValueError("cannot rank candidates without at least one query segment")

    tokenizer = getattr(reranker, "tokenizer", None)
    if tokenizer is None:
        raise ValueError("CrossEncoder reranker has no tokenizer")
    candidate_chunk_indices: list[int] = []
    for row in candidates:
        if "doc_id" not in row or "chunk_index" not in row:
            raise ValueError("candidate must contain doc_id and chunk_index")
        candidate_chunk_indices.append(int(row["chunk_index"]))
    bodies = load_bodies(candidate_chunk_indices)
    if not isinstance(bodies, Mapping):
        raise ValueError("load_bodies must return a mapping from chunk index to source text")
    missing = sorted(set(candidate_chunk_indices) - set(bodies))
    if missing:
        raise ValueError(f"reranker candidate chunk bodies are missing: {missing[:8]}")
    body_texts: list[str] = []
    for chunk_index in candidate_chunk_indices:
        body = bodies[chunk_index]
        if not isinstance(body, str):
            raise ValueError(f"chunk body {chunk_index} is not text")
        body_texts.append(body)
    query_texts: list[str] = []
    for segment_index, segment in enumerate(segments):
        if "text" not in segment or not isinstance(segment["text"], str):
            raise ValueError(f"query segment {segment_index} has no string text")
        query_texts.append(segment["text"])

    elapsed_started = time.perf_counter()
    source_query_cache: dict[Any, tuple[str, list[tuple[int, int]]]] = {}
    source_document_cache: dict[Any, tuple[str, list[tuple[int, int]]]] = {}
    best_by_doc: dict[str, float] = {}

    for candidate in candidates:
        doc_id = candidate["doc_id"]
        if not isinstance(doc_id, str):
            raise ValueError("candidate doc_id must be a string")
        best_by_doc.setdefault(doc_id, -math.inf)

    for segment_index, query_text in enumerate(query_texts):
        original_pairs = [(query_text, body) for body in body_texts]
        original_lengths = _pair_lengths(tokenizer, original_pairs, batch_size)
        if len(original_lengths) != len(candidates):
            raise ValueError("tokenizer pair count does not match candidate count")
        stats["max_original_input_tokens"] = max(
            stats["max_original_input_tokens"], max(original_lengths, default=0)
        )
        normal_positions = [i for i, length in enumerate(original_lengths) if length <= limit]
        overflow_positions = [i for i, length in enumerate(original_lengths) if length > limit]

        normal_pairs = [original_pairs[i] for i in normal_positions]
        normal_values = _predict(reranker, normal_pairs, batch_size)
        if len(normal_values) != len(normal_positions):
            raise ValueError("CrossEncoder normal-pair result count mismatch")
        for position, score in zip(normal_positions, normal_values):
            stats["expandedpairs"] += 1
            stats["max_input_tokens"] = max(stats["max_input_tokens"], original_lengths[position])
            doc_id = candidates[position]["doc_id"]
            best_by_doc[doc_id] = max(best_by_doc[doc_id], score)

        for position in overflow_positions:
            candidate = candidates[position]
            doc_id = candidate["doc_id"]
            chunk_index = candidate_chunk_indices[position]
            identity = f"segment={segment_index}, doc_id={doc_id!r}, chunk_index={chunk_index}"
            try:
                query_offsets = _source_cached(
                    source_query_cache, segment_index, tokenizer, query_text
                )
                document_offsets = _source_cached(
                    source_document_cache, chunk_index, tokenizer, body_texts[position]
                )
                query_windows, document_windows, window_max = _fit_overflow_windows(
                    tokenizer, query_text, body_texts[position], query_offsets,
                    document_offsets, batch_size=batch_size, limit=limit, overlap=overlap,
                )
            except Exception as exc:
                raise ValueError(f"failed to window overflowing pair ({identity}): {exc}") from exc

            coverage = stats["coverage"]
            query_token_count = len(query_offsets)
            document_token_count = len(document_offsets)
            query_char_count = len(query_text)
            document_char_count = len(body_texts[position])
            coverage["query_tokens_total"] += query_token_count
            coverage["query_tokens_covered"] += query_token_count
            coverage["document_tokens_total"] += document_token_count
            coverage["document_tokens_covered"] += document_token_count
            coverage["query_characters_total"] += query_char_count
            coverage["query_characters_covered"] += query_char_count
            coverage["document_characters_total"] += document_char_count
            coverage["document_characters_covered"] += document_char_count
            stats["query_windows"] += len(query_windows)
            stats["document_windows"] += len(document_windows)
            stats["overflowpairs"] += 1
            stats["max_input_tokens"] = max(stats["max_input_tokens"], window_max)

            pair_score = -math.inf
            for window_batch in _product_batches(query_windows, document_windows, batch_size):
                window_pairs = [(query_window["text"], document_window["text"])
                                for query_window, document_window in window_batch]
                window_scores = _predict(reranker, window_pairs, batch_size)
                stats["expandedpairs"] += len(window_pairs)
                if window_scores:
                    pair_score = max(pair_score, max(window_scores))
            if not math.isfinite(pair_score):
                raise ValueError(f"overflow pair produced no finite subwindow score ({identity})")
            best_by_doc[doc_id] = max(best_by_doc[doc_id], pair_score)

    coverage = stats["coverage"]
    coverage["query_token_fraction"] = (
        coverage["query_tokens_covered"] / coverage["query_tokens_total"]
        if coverage["query_tokens_total"] else 1.0
    )
    coverage["document_token_fraction"] = (
        coverage["document_tokens_covered"] / coverage["document_tokens_total"]
        if coverage["document_tokens_total"] else 1.0
    )
    coverage["query_character_fraction"] = (
        coverage["query_characters_covered"] / coverage["query_characters_total"]
        if coverage["query_characters_total"] else 1.0
    )
    coverage["document_character_fraction"] = (
        coverage["document_characters_covered"] / coverage["document_characters_total"]
        if coverage["document_characters_total"] else 1.0
    )
    coverage["complete"] = all(
        coverage[key] == coverage[key.replace("_covered", "_total")]
        for key in (
            "query_tokens_covered", "document_tokens_covered",
            "query_characters_covered", "document_characters_covered",
        )
    )
    if not coverage["complete"]:
        raise ValueError("overflow windowing did not cover every source token and character")
    if stats["max_input_tokens"] > limit:
        raise ValueError(
            f"windowing submitted an input of {stats['max_input_tokens']} tokens (limit {limit})"
        )

    ordered = sorted(candidates, key=lambda row: (-best_by_doc[row["doc_id"]], row["doc_id"]))
    elapsed_ms = (time.perf_counter() - elapsed_started) * 1000.0
    return ordered, best_by_doc, elapsed_ms, stats
