"""Headline clustering: merge stories about the same event so that one event
produces one notification and corroboration can be counted across sources."""
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Iterable, Optional, Sequence, Set, Tuple

# Character-level similarity is only trusted for near-verbatim duplicates;
# below this it mostly reflects a shared prefix such as
# "TON Foundation announces ..." and would merge unrelated stories.
NEAR_DUPLICATE_RATIO = 0.85
DEFAULT_THRESHOLD = 0.45
MIN_SHARED_TOKENS = 3

_STOPWORDS = {
    # EN
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "as", "by",
    "with", "from", "is", "are", "was", "were", "be", "been", "has", "have", "had",
    "it", "its", "this", "that", "these", "those", "after", "before", "over", "into",
    "about", "amid", "says", "said", "say", "new", "will", "could", "may", "might",
    "not", "no", "vs", "via", "than", "more", "most", "out", "how", "why", "what",
    "here", "their", "his", "her", "you", "your", "we", "our", "us", "they", "them",
    "today", "week", "year", "live", "report", "reports", "reportedly", "according",
    "analysis", "news", "update", "latest", "here's", "heres", "just", "now", "still",
    # RU
    "и", "в", "во", "на", "с", "со", "к", "ко", "по", "о", "об", "от", "до", "за",
    "из", "у", "не", "но", "а", "или", "что", "как", "это", "для", "при", "про", "же",
    "ли", "бы", "то", "так", "все", "всё", "ещё", "еще", "уже", "был", "была", "были",
    "было", "есть", "будет", "может", "после", "перед", "над", "под", "между", "через",
    "из-за", "также", "тоже", "свой", "свои", "его", "её", "их", "этот", "эта", "эти",
    "тот", "та", "те", "новый", "новая", "новые", "сегодня", "сообщает", "сообщил",
    "заявил", "заявила", "заявили",
}

# Nearly every relevant headline contains these; they carry no information
# about *which* event is being described.
_DOMAIN_STOPWORDS = {
    "ton", "toncoin", "gram", "grams", "telegram", "crypto", "cryptocurrency",
    "тон", "тонкоин", "грам", "телеграм", "крипто", "криптовалюта", "криптовалюты",
}

_LATIN_SUFFIXES = ("ing", "ed", "es", "ly", "s")
_TOKEN_RE = re.compile(r"[\w$%.-]+", re.UNICODE)
_PUNCT_RE = re.compile(r"[^\w\s$%]", re.UNICODE)

# Price-move verbs are near-interchangeable in headlines; collapse them so
# "TON jumps 20%" and "Toncoin surges 20%" share a token.
_SYNONYMS = {
    "jump": "up", "surge": "up", "soar": "up", "rally": "up", "climb": "up", "rise": "up",
    "gain": "up", "spike": "up", "pump": "up", "skyrocket": "up", "rocket": "up",
    "drop": "down", "fall": "down", "fell": "down", "plunge": "down", "tumble": "down",
    "slide": "down", "slump": "down", "dump": "down", "declin": "down", "crash": "down",
    "sink": "down", "plummet": "down", "dip": "down",
    "hack": "exploit", "breach": "exploit", "drain": "exploit",
}


def normalize_title(title: str) -> str:
    title = title.lower().replace("’", "'").replace("‘", "'")
    title = _PUNCT_RE.sub(" ", title)
    return re.sub(r"\s+", " ", title).strip()


def _stem(token: str) -> str:
    if re.search(r"[а-яё]", token):
        return token[:5] if len(token) > 5 else token
    for suffix in _LATIN_SUFFIXES:
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            token = token[: -len(suffix)]
            break
    token = token[:6]
    return _SYNONYMS.get(token, token)


def _significant_tokens(title: str) -> Set[str]:
    tokens = set()
    for raw in _TOKEN_RE.findall(normalize_title(title)):
        token = raw.strip(".-")
        if len(token) < 3 or token in _STOPWORDS or token in _DOMAIN_STOPWORDS:
            continue
        tokens.add(_stem(token))
    return tokens


def _token_overlap_score(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    shared = a & b
    required = 2 if min(len(a), len(b)) <= 3 else MIN_SHARED_TOKENS
    if len(shared) < required:
        return 0.0
    return len(shared) / len(a | b)


def similarity(a: str, b: str) -> float:
    """0..1 similarity combining token overlap and character similarity."""
    token_score = _token_overlap_score(_significant_tokens(a), _significant_tokens(b))
    ratio = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()
    seq_score = ratio if ratio >= NEAR_DUPLICATE_RATIO else ratio * 0.4
    return max(token_score, seq_score)


def find_matching_cluster(
    title: str,
    candidates: Iterable[Tuple[str, str]],
    threshold: float = DEFAULT_THRESHOLD,
) -> Optional[str]:
    """Return the id of the most similar candidate ``(cluster_id, title)``."""
    best_id: Optional[str] = None
    best_score = threshold
    for cluster_id, candidate_title in candidates:
        score = similarity(title, candidate_title)
        if score >= best_score:
            best_score = score
            best_id = cluster_id
    return best_id


def new_cluster_id(title: str, bucket: str) -> str:
    basis = f"{normalize_title(title)}|{bucket}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
