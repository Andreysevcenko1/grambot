"""Cluster similar news items so duplicate/corroborating reports from
multiple sources are grouped instead of triggering repeat notifications.
"""
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import List, Optional, Sequence

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+")


def normalize_title(title: str) -> str:
    words = _WORD_RE.findall(title.lower())
    return " ".join(words)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def find_matching_cluster(
    title: str,
    candidate_titles: Sequence[tuple[str, str]],
    threshold: float = 0.6,
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
