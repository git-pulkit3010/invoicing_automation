"""Person-name normalisation and roster matching.

Matching a PDF name onto a timesheet row has to be exact enough to keep
"Chaitanya Motupalli" away from "Chaitanya Kumar Nara" and "Harish Giriraju"
away from "Harish Sugguna". Token-set equality gives that: two names match
only when they contain exactly the same name tokens, regardless of case,
punctuation or word order.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")


def normalize_name(name: str) -> str:
    """Upper-case, strip punctuation and collapse whitespace."""
    cleaned = _NON_ALNUM.sub(" ", (name or "").upper())
    return " ".join(cleaned.split())


def name_tokens(name: str) -> frozenset[str]:
    """The set of name tokens, used as the matching key."""
    return frozenset(normalize_name(name).split())


class NameRoster:
    """Lookup of known contractors, matched on token set."""

    def __init__(self) -> None:
        self._by_tokens: dict[frozenset[str], str] = {}
        self._by_normalized: dict[str, str] = {}

    def add(self, display_name: str, key: str) -> None:
        self._by_tokens[name_tokens(display_name)] = key
        self._by_normalized[normalize_name(display_name)] = key

    def __len__(self) -> int:
        return len(self._by_tokens)

    def match(self, candidate: str) -> Optional[str]:
        """Return the roster key for an exactly matching name, else None."""
        if not candidate:
            return None
        normalized = normalize_name(candidate)
        if normalized in self._by_normalized:
            return self._by_normalized[normalized]
        tokens = name_tokens(candidate)
        if tokens in self._by_tokens:
            return self._by_tokens[tokens]
        return None

    def find_in_text(self, words: Iterable[str]) -> Optional[tuple[str, str]]:
        """Find the longest roster name inside a sequence of words.

        Returns ``(matched_name, key)`` or None. Longest-match wins so that
        "Consulting Vaibhav Bansal" resolves to "Vaibhav Bansal" rather than a
        shorter accidental substring.
        """
        words = list(words)
        best: Optional[tuple[str, str]] = None
        best_len = 0
        for start in range(len(words)):
            for end in range(start + 1, len(words) + 1):
                span = words[start:end]
                key = self.match(" ".join(span))
                if key and (end - start) > best_len:
                    best = (" ".join(span), key)
                    best_len = end - start
        return best


def similarity(a: str, b: str) -> float:
    """Token-overlap similarity, used only for diagnostics."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
