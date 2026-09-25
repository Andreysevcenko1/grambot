"""Rule-based sentiment / strength classifier.

This is a deliberately conservative heuristic, not a prediction engine: it
labels a headline as likely positive / negative / neutral / unknown and
estimates how strong the signal could be. The LLM classifier (optional)
builds on the same ``Classification`` model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Protocol, Tuple

from .keywords import compile_keyword

# Points per matched term. "*" suffix = prefix match (see keywords.py).
_STRONG = 3
_MILD = 1
_ENTITY = 1

NEGATIVE_STRONG = [
    "exploit*", "hack", "hacked", "hacker*", "breach*", "drained", "stolen", "theft",
    "rug pull", "delist*", "halt*", "outage", "downtime", "network down", "chain stopped",
    "ban", "banned", "bans", "blocked", "lawsuit", "sued", "sues", "charges", "indict*",
    "arrest*", "sanction*", "insolven*", "bankrupt*", "crash*", "plunge*", "plummet*",
    "collapse*", "liquidation*", "attack*", "malware", "layoff*", "shut down", "shuts down",
    "shutdown", "frozen", "freeze*", "seized", "seizure",
    # RU
    "взлом*", "эксплойт*", "украден*", "украли", "кража", "делистинг*", "остановк*",
    "сбой", "сбои", "падение сети", "арест*", "иск", "иски", "запрет*", "заблокир*",
    "санкци*", "банкрот*", "обвал*", "крах", "атак*", "ликвидаци*", "увольнени*",
]

NEGATIVE_MILD = [
    "vulnerab*", "bug", "bugs", "incident", "investigat*", "probe", "regulator*",
    "fine", "fined", "penalt*", "warning", "warns", "scam*", "phishing", "fraud*",
    "dump*", "sell-off", "selloff", "decline*", "drop", "drops", "dropped", "fall",
    "falls", "fell", "tumble*", "slide*", "slump*", "down", "bearish", "resign*",
    "delay*", "postpone*", "risk", "risks", "concern*", "loses", "lost", "losses",
    "outflow*", "fear*", "pressure", "weak*",
    # RU
    "уязвимост*", "расследован*", "штраф*", "мошенн*", "фишинг", "падени*", "падает",
    "упал*", "снижени*", "снижается", "распродаж*", "задерж*", "отставк*", "риск*",
    "опасен*", "потер*", "убыт*", "медвеж*",
]

POSITIVE_STRONG = [
    "listing", "listed", "to list", "will list", "lists", "ETF", "mainnet", "partnership*",
    "partners with", "acquisition", "acquire*", "launch", "launches", "launched",
    "approved", "approval", "approves", "record high", "all-time high", "ATH",
    "surge*", "soar*", "jumps", "jumped", "skyrocket*", "breakout", "integrat*",
    "adopt*", "million users", "billion users",
    # RU
    "листинг*", "запуск*", "запустил*", "партнёрств*", "партнерств*", "интеграци*",
    "одобр*", "рекорд*", "максимум*", "взлетел*", "прорыв*", "внедрени*",
]

POSITIVE_MILD = [
    "upgrade*", "update", "updates", "grant", "grants", "funding", "invest*", "raised",
    "raises", "milestone", "wallet", "staking", "airdrop*", "bullish", "gain", "gains",
    "climb*", "rise", "rises", "rising", "rose", "up", "recover*", "rebound*", "rally",
    "rallies", "expand*", "expansion", "growth", "grows", "support", "supports",
    "inflow*", "buyback", "burn", "burns",
    # RU
    "обновлени*", "грант*", "инвестиц*", "инвестиру*", "привлек*", "привлёк*",
    "стейкинг*", "аирдроп*", "кошел*", "рост", "растёт", "растет", "выросл*",
    "ралли", "восстанов*", "расширени*", "поддержк*", "бычь*",
]

# Big names near a signal word make it more likely to matter.
ENTITIES = [
    "Binance", "Coinbase", "OKX", "Bybit", "Kraken", "Bitget", "KuCoin", "HTX", "Upbit",
    "SEC", "CFTC", "Telegram", "TON Foundation", "Durov", "Tether", "USDT", "Pantera",
    "BlackRock", "Grayscale", "Visa", "Mastercard", "Apple", "Google",
    "Бинанс", "Телеграм", "Дуров",
]

_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s?%")
_BILLION_RE = re.compile(r"\b(billion|bn|млрд)\b", re.IGNORECASE)
_MILLION_RE = re.compile(r"\b(million|mln|млн)\b", re.IGNORECASE)

STRENGTH_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Classification:
    sentiment: str  # positive | negative | neutral | unknown
    strength: str  # low | medium | high
    matched_keywords: List[str] = field(default_factory=list)
    reason: str = ""


class Classifier(Protocol):
    def classify(self, text: str) -> Classification:  # pragma: no cover - protocol
        ...


def _score(text: str, strong: Iterable[str], mild: Iterable[str]) -> Tuple[int, List[str]]:
    score = 0
    matched: List[str] = []
    for term in strong:
        if compile_keyword(term).search(text):
            score += _STRONG
            matched.append(term.rstrip("*"))
    for term in mild:
        if compile_keyword(term).search(text):
            score += _MILD
            matched.append(term.rstrip("*"))
    return score, matched


def largest_percent(text: str) -> float:
    values = []
    for raw in _PERCENT_RE.findall(text):
        try:
            values.append(float(raw.replace(",", ".")))
        except ValueError:
            continue
    return max(values) if values else 0.0


def strength_from_score(score: int) -> str:
    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


class RuleBasedClassifier:
    """Lexicon-driven classifier with word-boundary matching (EN + RU)."""

    def classify(self, text: str) -> Classification:
        neg_score, neg_terms = _score(text, NEGATIVE_STRONG, NEGATIVE_MILD)
        pos_score, pos_terms = _score(text, POSITIVE_STRONG, POSITIVE_MILD)

        if pos_score == 0 and neg_score == 0:
            return Classification(sentiment="neutral", strength="low", matched_keywords=[], reason="")

        if pos_score > neg_score:
            sentiment, base, terms = "positive", pos_score, pos_terms
        elif neg_score > pos_score:
            sentiment, base, terms = "negative", neg_score, neg_terms
        else:
            # Mixed signals of equal weight: flag for manual review.
            sentiment, base, terms = "unknown", pos_score, pos_terms + neg_terms

        boosts: List[str] = []
        pct = largest_percent(text)
        if pct >= 10:
            base += 2
            boosts.append(f"{pct:g}%")
        elif pct >= 5:
            base += 1
            boosts.append(f"{pct:g}%")

        if _BILLION_RE.search(text):
            base += 2
            boosts.append("billions")
        elif _MILLION_RE.search(text):
            base += 1
            boosts.append("millions")

        entity_hits = [e for e in ENTITIES if compile_keyword(e).search(text)]
        base += min(len(entity_hits), 2) * _ENTITY

        reason_parts = terms[:4] + boosts + entity_hits[:2]
        return Classification(
            sentiment=sentiment,
            strength=strength_from_score(base),
            matched_keywords=terms,
            reason=", ".join(dict.fromkeys(reason_parts)),
        )


def meets_min_strength(strength: str, minimum: str) -> bool:
    return STRENGTH_ORDER.get(strength, 0) >= STRENGTH_ORDER.get(minimum, 0)
