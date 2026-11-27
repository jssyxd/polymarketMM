from btc5m.backfill import parse_clob


def test_parse_clob_uses_winner():
    row = {"closed": True, "tokens": [{"outcome": "Up", "price": 0.0, "winner": False}, {"outcome": "Down", "price": 1.0, "winner": True}]}
    assert parse_clob(row) == (0.0, 1.0, "Down")


def test_parse_clob_ignores_open_market():
    row = {"closed": False, "tokens": [{"outcome": "Up", "price": 0.2}, {"outcome": "Down", "price": 0.8}]}
    assert parse_clob(row) is None
