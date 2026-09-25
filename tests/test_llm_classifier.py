from unittest.mock import patch

from grambot.processing.classifier import RuleBasedClassifier
from grambot.processing.llm_classifier import LLMClassifier


def _mock_response(content: str):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    return _Resp()


def test_llm_classifier_parses_valid_json_response():
    classifier = LLMClassifier(api_key="fake-key")
    with patch("grambot.processing.llm_classifier.requests.post") as mock_post:
        mock_post.return_value = _mock_response('{"sentiment": "negative", "strength": "high"}')
        result = classifier.classify("TON Foundation confirms exploit")

    assert result.sentiment == "negative"
    assert result.strength == "high"


def test_llm_classifier_tolerates_extra_text_around_json():
    classifier = LLMClassifier(api_key="fake-key")
    with patch("grambot.processing.llm_classifier.requests.post") as mock_post:
        mock_post.return_value = _mock_response(
            'Sure, here is the result:\n{"sentiment": "positive", "strength": "medium"}\nDone.'
        )
        result = classifier.classify("TON partners with major exchange")

    assert result.sentiment == "positive"
    assert result.strength == "medium"


def test_llm_classifier_falls_back_on_invalid_sentiment_value():
    classifier = LLMClassifier(api_key="fake-key")
    with patch("grambot.processing.llm_classifier.requests.post") as mock_post:
        mock_post.return_value = _mock_response('{"sentiment": "bullish", "strength": "extreme"}')
        result = classifier.classify("Some headline")

    assert result.sentiment == "unknown"
    assert result.strength == "low"


def test_llm_classifier_falls_back_to_rule_based_on_api_error():
    fallback = RuleBasedClassifier()
    classifier = LLMClassifier(api_key="fake-key", fallback=fallback)
    with patch("grambot.processing.llm_classifier.requests.post", side_effect=Exception("network error")):
        result = classifier.classify("TON Foundation confirms network exploit, funds at risk")

    assert result.sentiment == "negative"
    assert result.strength == "high"
