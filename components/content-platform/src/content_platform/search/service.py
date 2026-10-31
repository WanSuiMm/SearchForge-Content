"""Validated FTS5 content search."""

import re

from ..contracts import ContentRecord
from ..errors import ValidationError
from ..storage.repositories import SearchRepository


class SearchService:
    def __init__(self, repository: SearchRepository) -> None:
        self.repository = repository

    def search(self, query: object, *, limit: int = 20) -> list[ContentRecord]:
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > 200:
            raise ValidationError("query must contain 1 to 200 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValidationError("limit must be an integer from 1 to 100")
        terms = re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)
        if not terms or len(terms) > 20:
            raise ValidationError("query must contain 1 to 20 searchable terms")
        # Quote each token so user supplied FTS operators cannot change the query plan.
        match_expression = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        return self.repository.search(match_expression, limit=limit)
