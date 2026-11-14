"""Stable extraction of the best two matching passages from each document."""

from __future__ import annotations

import re
from unicodedata import normalize

from .contracts import EvidencePassage
from .storage import digest


PASSAGE_VERSION = "passage_selection_v2.0"


def keywords(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[^\W_]+", normalize("NFKC", text).lower())
        if len(token) > 1
    }


def passages(documents, limit):
    result = []
    for document, text, query_texts in documents:
        terms = set().union(*(keywords(query) for query in query_texts))
        ranges = []
        for paragraph in re.finditer(r"\S(?:.*?\S)?(?=\n[ \t]*\n|\Z)", text, flags=re.DOTALL):
            start, end = paragraph.span()
            while start < end:
                stop = min(start + 1500, end)
                score = len(terms.intersection(keywords(text[start:stop])))
                ranges.append((-score, start, stop))
                start = stop
        for score, start, end in sorted(ranges)[:2]:
            if not limit:
                return tuple(result)
            end = min(end, start + limit)
            item = EvidencePassage(
                evidence_id=digest([PASSAGE_VERSION, document.document_id, start, end])[:32],
                document_id=document.document_id,
                text=text[start:end],
                start=start,
                end=end,
                keyword_matches=-score,
            )
            result.append(item)
            limit -= end - start
    return tuple(result)
