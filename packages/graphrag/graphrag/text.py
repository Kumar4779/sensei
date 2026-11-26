"""Tokenization shared by BM25, the hashing embedder and the answer composer."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a about above after all also an and any are as at be been being but by can could did do does for from had has "
    "have how i if in into is it its just let me many much my no not of on or our please s so some tell than that "
    "the their them then there these they this those to up us was we were what whats when where which who whom why "
    "will with would you your".split()
)


def _stem(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lower-case alphanumeric tokens, stopwords dropped, trailing plural 's' stripped."""
    return [_stem(t) for t in _TOKEN_RE.findall(text.casefold()) if t not in STOPWORDS and len(t) > 1]
