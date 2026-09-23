"""Cluster similar news items so duplicate/corroborating reports from
multiple sources are grouped instead of triggering repeat notifications.
"""
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import FrozenSet, List, Optional, Sequence

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+")

# Common function words that carry little topical meaning. Filtering these
# out before computing token overlap keeps the comparison focused on the
# words that actually identify the story (entities, actions, numbers), so
# two outlets phrasing the same event differently ("X confirms Y" vs.
# "Y confirmed by X") still overlap heavily.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
        "is", "are", "was", "were", "by", "with", "after", "amid", "as",
        "from", "over", "its", "that", "this", "said", "says", "say",
        "will", "has", "have", "had", "be", "been", "new", "than",
    }
)

# Minimum number of shared significant tokens required before token overlap
# alone can be trusted as a match; guards against short titles coincidentally
# sharing one or two generic words.
_MIN_SHARED_TOKENS = 3


def normalize_title(title: str) -> str:
    words = _WORD_RE.findall(title.lower())
    return " ".join(words)


def _significant_tokens(title: str) -> FrozenSet[str]:
    words = _WORD_RE.findall(title.lower())
    return frozenset(w for w in words if len(w) >= 3 and w not in _STOPWORDS)


def _token_overlap_score(a: str, b: str) -> float:
    tokens_a = _significant_tokens(a)
    tokens_b = _significant_tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    shared = tokens_a & tokens_b
    if len(shared) < min(_MIN_SHARED_TOKENS, len(tokens_a), len(tokens_b)):
        return 0.0
    union = tokens_a | tokens_b
    return len(shared) / len(union)


def similarity(a: str, b: str) -> float:
    """Combined similarity score in [0, 1].

    Takes the best of two signals: character-sequence similarity (catches
    near-identical titles/typo-level differences) and significant-token
    overlap (catches the same event phrased differently across outlets,
    e.g. reordered clauses or active/passive voice).
    """
    sequence_score = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()
    token_score = _token_overlap_score(a, b)
    return max(sequence_score, token_score)


def find_matching_cluster(
    title: str,
    candidate_titles: Sequence[tuple[str, str]],
    threshold: float = 0.45,
) -> Optional[str]:
    """Return the cluster_id of the best matching candidate above threshold.

    ``candidate_titles`` is a sequence of (cluster_id, representative_title).
    """
    best_id: Optional[str] = None
    best_score = 0.0
    for cluster_id, candidate_title in candidate_titles:
        score = similarity(title, candidate_title)
        if score >= threshold and score > best_score:
            best_score = score
            best_id = cluster_id
    return best_id


def new_cluster_id(title: str, published_bucket: str) -> str:
    basis = f"{normalize_title(title)}|{published_bucket}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

