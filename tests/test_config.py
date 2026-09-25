from grambot.config import Settings


def test_display_currency_parsing(monkeypatch):
    monkeypatch.setenv("DISPLAY_CURRENCY", " eur ")
    assert Settings.from_env().display_currency == "EUR"
    monkeypatch.setenv("DISPLAY_CURRENCY", "")
    assert Settings.from_env().display_currency == "USD"
    monkeypatch.setenv("DISPLAY_CURRENCY", "euro")
    assert Settings.from_env().display_currency == "USD"
