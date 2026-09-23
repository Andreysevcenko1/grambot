"""News classification: sentiment + signal strength.

The classifier is intentionally rule-based so the bot works out of the box
without any paid API key. It is designed as a small, swappable interface
(`Classifier` protocol) so a future LLM-backed implementation can be dropped
in without touching the rest of the pipeline -- the task explicitly warns
that impact estimation can never be guaranteed to be accurate, so this
module optimizes for fast, explainable, conservative heuristics rather than
false confidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol

STRENGTH_ORDER = {"low": 0, "medium": 1, "high": 2}

POSITIVE_KEYWORDS = [
    "partnership",
    "listing",
    "listed",
    "integration",
    "launch",
    "upgrade",
    "record high",
    "adoption",
    "grant",
    "funding round",
    "mainnet",
    "milestone",
]

NEGATIVE_KEYWORDS = [
    "hack",
    "hacked",
    "exploit",
    "vulnerability",
    "delist",
    "delisting",
    "ban",
    "banned",
    "lawsuit",
    "sec ",
    "regulation",
    "regulatory",
    "outage",
    "halt",
    "halted",
    "frozen",
    "freeze",
    "scam",
    "rug pull",
    "investigation",
    "breach",
]

HIGH_IMPACT_KEYWORDS = [
    "hack",
    "hacked",
    "exploit",
    "vulnerability",
    "delist",
    "delisting",
    "ban",
    "banned",
    "lawsuit",
    "sec ",
    "regulation",
    "halt",
    "halted",
    "frozen",
    "breach",
    "listing",
    "listed",
]

MEDIUM_IMPACT_KEYWORDS = [
    "partnership",
    "integration",
    "upgrade",
    "update",
    "mainnet",
    "outage",
]


@dataclass
class Classification:
    sentiment: str  # "positive" | "negative" | "neutral" | "unknown"
    strength: str  # "low" | "medium" | "high"
    matched_keywords: List[str]


class Classifier(Protocol):
    def classify(self, text: str) -> Classification:
        ...


class RuleBasedClassifier:
    """Simple, deterministic keyword-driven classifier."""

    def classify(self, text: str) -> Classification:
        lowered = text.lower()

        matched_negative = [kw for kw in NEGATIVE_KEYWORDS if kw in lowered]
        matched_positive = [kw for kw in POSITIVE_KEYWORDS if kw in lowered]

        if matched_negative and matched_positive:
            sentiment = "unknown"
        elif matched_negative:
            sentiment = "negative"
        elif matched_positive:
            sentiment = "positive"
        else:
            sentiment = "neutral"

        matched_high = [kw for kw in HIGH_IMPACT_KEYWORDS if kw in lowered]
        matched_medium = [kw for kw in MEDIUM_IMPACT_KEYWORDS if kw in lowered]

        if matched_high:
            strength = "high"
        elif matched_medium:
            strength = "medium"
        else:
            strength = "low"

        matched_keywords = sorted(set(matched_negative + matched_positive + matched_high + matched_medium))
        return Classification(sentiment=sentiment, strength=strength, matched_keywords=matched_keywords)


def meets_min_strength(strength: str, min_strength: str) -> bool:
    return STRENGTH_ORDER.get(strength, 0) >= STRENGTH_ORDER.get(min_strength, 0)
