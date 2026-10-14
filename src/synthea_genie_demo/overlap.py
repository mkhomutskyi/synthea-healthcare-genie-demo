"""Exact phrase overlap between payload-held-out paraphrases and embedded or regression text.

A held-out paraphrase should test whether a boundary class carries over to new wording,
so it must not reuse a distinctive phrase from the Agent instructions, the regression
probes or the benchmark questions (for example "lowest quality" or "best to worst").

Text is split into clauses at punctuation, and function words are dropped, so "best to
worst" and "best or worst" are the same phrase. A distinctive phrase is two or three
consecutive content words with at least one word outside the shared domain vocabulary
below; shared domain vocabulary (metric, dimension and data words such as "claim cost",
"payer category" or "2025 encounters") never counts, so ordinary healthcare nouns do not
fail the check. The single-word examples the Boundaries section lists in parentheses
(such as "best", "worst" and "top") also count as distinctive phrases.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_SHARED_PHRASES = 0
FUNCTION_WORDS = frozenset(
    [
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "each",
        "every",
        "give",
        "list",
        "show",
        "tell",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "more",
        "most",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "per",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "would",
        "you",
        "your",
    ]
)
DOMAIN_WORDS = frozenset(
    [
        "aggregate",
        "amount",
        "amounts",
        "average",
        "billed",
        "calendar",
        "categories",
        "category",
        "claim",
        "claims",
        "class",
        "classes",
        "cost",
        "costs",
        "count",
        "counts",
        "coverage",
        "covered",
        "data",
        "dataset",
        "distinct",
        "encounter",
        "encounters",
        "health",
        "insurance",
        "insurer",
        "insurers",
        "measure",
        "measures",
        "metric",
        "metrics",
        "number",
        "patient",
        "patients",
        "payer",
        "payers",
        "period",
        "periods",
        "quarter",
        "quarters",
        "rate",
        "rates",
        "simulated",
        "synthetic",
        "total",
        "totals",
        "unique",
        "visit",
        "visits",
        "year",
        "years",
    ]
)
WORD_RE = re.compile(r"[a-z0-9]+")
CLAUSE_RE = re.compile(r"[.,;:?!()\[\]\"]+")
PARENTHETICAL_RE = re.compile(r"\(([^()]*)\)")
BOUNDARIES_HEADING = "Boundaries"


def _domain(word: str) -> bool:
    return word in DOMAIN_WORDS or word.isdigit()


def _clauses(text: str) -> list[list[str]]:
    return [
        [w for w in WORD_RE.findall(clause) if w not in FUNCTION_WORDS]
        for clause in CLAUSE_RE.split(text.lower())
    ]


def distinctive_phrases(text: str) -> set[str]:
    found: set[str] = set()
    for tokens in _clauses(text):
        for size in (2, 3):
            for start in range(len(tokens) - size + 1):
                window = tokens[start : start + size]
                if not all(_domain(w) for w in window):
                    found.add(" ".join(window))
    return found


def boundary_example_words(instructions: str) -> set[str]:
    """Single-word examples listed in parentheses in the instructions' Boundaries section."""
    section = instructions.split(f"\n{BOUNDARIES_HEADING}\n", 1)
    if len(section) != 2:
        return set()
    found: set[str] = set()
    for inner in PARENTHETICAL_RE.findall(section[1]):
        for item in inner.lower().split(","):
            item_words = WORD_RE.findall(item)
            if len(item_words) == 1 and not _domain(item_words[0]):
                found.add(item_words[0])
    return found


def shared_phrases(
    text: str, corpus: Iterable[str], example_words: Iterable[str] = ()
) -> list[str]:
    """Distinctive phrases of ``text`` that also occur, word for word, in ``corpus``."""
    seen: set[str] = set()
    for source in corpus:
        seen |= distinctive_phrases(source)
    words = {w for tokens in _clauses(text) for w in tokens}
    return sorted((distinctive_phrases(text) & seen) | (words & set(example_words)))
