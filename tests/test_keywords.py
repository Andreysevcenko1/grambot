from grambot.processing.keywords import compile_keyword, matches_any


def test_prefix_match_with_star():
    pattern = compile_keyword("взлом*")
    assert pattern.search("Биржу взломали ночью")
    assert pattern.search("Взлом подтверждён")
    assert not pattern.search("невзломанный")  # no boundary before


def test_short_uppercase_is_case_sensitive():
    assert compile_keyword("SEC").search("SEC charges firm")
    assert not compile_keyword("SEC").search("second attempt")
    assert not compile_keyword("SEC").search("sec charges")


def test_longer_words_are_case_insensitive():
    assert compile_keyword("hack").search("HACK confirmed")
    assert not compile_keyword("hack").search("hackathon")


def test_matches_any_preserves_order():
    assert matches_any("Telegram and TON", ["TON", "Telegram", "GRAM"]) == ["TON", "Telegram"]
