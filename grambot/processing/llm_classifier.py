"""LLM-backed news classifier.

Uses any OpenAI-compatible chat completions API to estimate sentiment and
signal strength for a news item. This is strictly a *best-effort estimate*
-- as the design brief stresses, impact assessment can never be guaranteed
accurate, so this classifier is deliberately conservative (defaults to
"unknown"/"low" on any ambiguity or failure) and always falls back to the
deterministic `RuleBasedClassifier` if the API key is missing, the request
fails, or the response can't be parsed. This keeps the bot fully functional
without any paid API key, while allowing better judgement when one is
configured.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from .classifier import Classification, RuleBasedClassifier

logger = logging.getLogger(__name__)

_VALID_SENTIMENTS = {"positive", "negative", "neutral", "unknown"}
_VALID_STRENGTHS = {"low", "medium", "high"}

_SYSTEM_PROMPT = (
    "You are a conservative financial news triage assistant for a "
    "TON/Toncoin/GRAM monitoring bot. Given a news headline and summary, "
    "classify its likely short-term market impact. Respond ONLY with a "
    "compact JSON object with exactly these keys: "
    '{"sentiment": "positive"|"negative"|"neutral"|"unknown", '
    '"strength": "low"|"medium"|"high", "reason": "<= 12 words in Russian"}. '
    "Use \"unknown\" sentiment when the impact is genuinely unclear or "
    "mixed, and prefer lower strength when uncertain. Rebrands, price "
    "recaps and opinion pieces are neutral. Do not include markdown or any "
    "text outside the JSON object."
)


class LLMClassifier:
    """Classifier backed by an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout: float = 15.0,
        fallback: Optional[RuleBasedClassifier] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.fallback = fallback or RuleBasedClassifier()

    def classify(self, text: str) -> Classification:
        try:
            return self._classify_via_api(text)
        except Exception:  # pragma: no cover - defensive, network dependent
            logger.exception("LLM classification failed, falling back to rule-based classifier")
            return self.fallback.classify(text)

    def _classify_via_api(self, text: str) -> Classification:
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text[:4000]},
                ],
                "temperature": 0,
                "max_tokens": 120,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"].strip()
        parsed = _parse_json_object(content)

        sentiment = str(parsed.get("sentiment", "unknown")).lower()
        strength = str(parsed.get("strength", "low")).lower()
        reason = str(parsed.get("reason", "") or "").strip()[:200]

        if sentiment not in _VALID_SENTIMENTS:
            sentiment = "unknown"
        if strength not in _VALID_STRENGTHS:
            strength = "low"

        # The LLM path doesn't produce matched keywords; keep the field for
        # interface compatibility but leave it empty.
        return Classification(sentiment=sentiment, strength=strength, matched_keywords=[], reason=reason)


def _parse_json_object(content: str) -> dict:
    """Best-effort JSON extraction, tolerant of stray text around the object."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(content[start : end + 1])
        raise
