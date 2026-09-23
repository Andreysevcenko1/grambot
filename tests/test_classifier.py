from grambot.processing.classifier import RuleBasedClassifier, meets_min_strength


def test_negative_high_impact():
    classifier = RuleBasedClassifier()
    result = classifier.classify("TON Foundation confirms network exploit, funds at risk")
    assert result.sentiment == "negative"
    assert result.strength == "high"


def test_positive_medium_impact():
    classifier = RuleBasedClassifier()
    result = classifier.classify("Major exchange announces new TON partnership and integration")
    assert result.sentiment == "positive"
    assert result.strength == "medium"


def test_neutral_low_impact():
    classifier = RuleBasedClassifier()
    result = classifier.classify("TON developer community publishes weekly newsletter")
    assert result.sentiment == "neutral"
    assert result.strength == "low"


def test_meets_min_strength():
    assert meets_min_strength("high", "low")
    assert meets_min_strength("medium", "medium")
    assert not meets_min_strength("low", "high")
