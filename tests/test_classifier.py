from grambot.processing.classifier import RuleBasedClassifier, largest_percent, meets_min_strength

classifier = RuleBasedClassifier()


def test_negative_high_impact():
    result = classifier.classify("TON Foundation confirms network exploit, funds at risk")
    assert result.sentiment == "negative"
    assert result.strength == "high"
    assert "exploit" in result.matched_keywords
    assert result.reason


def test_positive_strong_terms_give_high_strength():
    result = classifier.classify("Major exchange announces new TON partnership and integration")
    assert result.sentiment == "positive"
    assert result.strength == "high"


def test_positive_mild_terms_give_medium_strength():
    result = classifier.classify("Pavel Durov announces Gram wallet")
    assert result.sentiment == "positive"
    assert result.strength == "medium"


def test_neutral_low_impact():
    result = classifier.classify("TON developer community publishes weekly newsletter")
    assert result.sentiment == "neutral"
    assert result.strength == "low"
    assert result.reason == ""


def test_word_boundaries_prevent_false_hits():
    assert classifier.classify("TON Hackathon draws 500 developers").sentiment == "neutral"
    assert classifier.classify("Urban planning in Toronto").sentiment == "neutral"


def test_percent_move_boosts_strength():
    result = classifier.classify("Toncoin jumps 36% as Telegram takes over TON chain")
    assert result.sentiment == "positive"
    assert result.strength == "high"
    assert "36%" in result.reason


def test_dollar_amount_boosts_strength():
    result = classifier.classify("TON bridge exploited for $50 million")
    assert result.sentiment == "negative"
    assert result.strength == "high"


def test_russian_headlines_are_classified():
    result = classifier.classify("Telegram заблокировали в России, TON упал на 12%")
    assert result.sentiment == "negative"
    assert result.strength == "high"
    positive = classifier.classify("Binance объявила листинг GRAM")
    assert positive.sentiment == "positive"


def test_mixed_signals_are_unknown():
    result = classifier.classify("TON rallies after hack")
    # rally (mild, +1) vs hack (strong, +3): negative wins
    assert result.sentiment == "negative"
    mixed = classifier.classify("TON listing delisted")
    assert mixed.sentiment == "unknown"


def test_largest_percent():
    assert largest_percent("up 3.5% then 12%") == 12.0
    assert largest_percent("no numbers") == 0.0
    assert largest_percent("вырос на 4,2%") == 4.2


def test_meets_min_strength():
    assert meets_min_strength("high", "low")
    assert meets_min_strength("medium", "medium")
    assert not meets_min_strength("low", "high")
