from grambot.app import build_classifier
from grambot.config import Settings
from grambot.processing.classifier import RuleBasedClassifier
from grambot.processing.llm_classifier import LLMClassifier


def test_build_classifier_uses_rule_based_by_default():
    settings = Settings(openai_api_key="")
    classifier = build_classifier(settings)
    assert isinstance(classifier, RuleBasedClassifier)


def test_build_classifier_uses_llm_when_api_key_configured():
    settings = Settings(openai_api_key="fake-key", openai_model="gpt-4o-mini")
    classifier = build_classifier(settings)
    assert isinstance(classifier, LLMClassifier)
    assert classifier.model == "gpt-4o-mini"
