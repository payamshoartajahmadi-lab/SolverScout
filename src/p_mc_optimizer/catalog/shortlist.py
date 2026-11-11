"""Deterministic, bounded FTS retrieval from qualified catalog cards."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass

from p_mc_optimizer.catalog.models import CatalogSnapshot, SolverCardV2


@dataclass(frozen=True)
class Shortlist:
    cards: tuple[SolverCardV2, ...]
    excluded: tuple[dict[str, str], ...]


def _terms(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    return list(dict.fromkeys(re.findall(r"[^\W_]{2,}", text)))[:64]


def _fts_ranks(cards: list[SolverCardV2], query: str) -> dict[tuple[str, str], float]:
    terms = _terms(query)
    if not terms or not cards:
        return {}
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE card_index USING fts5(implementation_id UNINDEXED, config_id UNINDEXED, terms)"
        )
        connection.executemany(
            "INSERT INTO card_index VALUES (?,?,?)",
            ((card.implementation_id, card.config_id, " ".join(_terms(" ".join((
                card.implementation_id, card.seed_identity, card.family_id,
            ))))) for card in cards),
        )
        pattern = " OR ".join(f'"{term}"' for term in terms)
        return {
            (implementation_id, config_id): rank for implementation_id, config_id, rank in connection.execute(
                "SELECT implementation_id, config_id, bm25(card_index) FROM card_index WHERE card_index MATCH ?",
                (pattern,),
            )
        }


def shortlist(snapshot: CatalogSnapshot, *, dimension: int, query: str = "", limit: int = 20,
              numeric_exclusions: dict[str, str] | None = None) -> Shortlist:
    if not 1 <= limit <= 20:
        raise ValueError("A shortlist contains at most twenty cards")
    eligible, excluded = [], []
    for card in snapshot.cards:
        if numeric_exclusions and card.config_id in numeric_exclusions:
            excluded.append({"implementation_id": card.implementation_id, "config_id": card.config_id,
                             "reason": numeric_exclusions[card.config_id]})
        elif not card.min_dimension <= dimension <= card.max_dimension or not card.pilot_500_compatible:
            excluded.append({
                "implementation_id": card.implementation_id, "config_id": card.config_id,
                "reason": "INCOMPATIBLE_DOMAIN" if not card.min_dimension <= dimension <= card.max_dimension
                else "PILOT_500_UNQUALIFIED",
            })
        else:
            eligible.append(card)
    ranks = _fts_ranks(eligible, query)
    eligible.sort(key=lambda card: (
        ranks.get((card.implementation_id, card.config_id), 0.0), card.implementation_id, card.config_id,
    ))
    representatives, remainder, families, implementations = [], [], set(), set()
    for card in eligible:
        if card.implementation_id in implementations:
            excluded.append({"implementation_id": card.implementation_id, "config_id": card.config_id,
                             "reason": "ALTERNATE_CONFIGURATION"})
            continue
        implementations.add(card.implementation_id)
        if card.family_id in families:
            remainder.append(card)
        else:
            representatives.append(card)
            families.add(card.family_id)
    ordered = representatives + remainder
    excluded.extend(
        {"implementation_id": card.implementation_id, "config_id": card.config_id, "reason": "SHORTLIST_LIMIT"}
        for card in ordered[limit:]
    )
    return Shortlist(tuple(ordered[:limit]), tuple(excluded))
