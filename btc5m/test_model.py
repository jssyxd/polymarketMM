import json
from btc5m.model import Level, OkxBook, Window, parse_levels, top
from btc5m.recorder import OkxCache, _parse_clob_resolution, _parse_poly_trade, _parse_resolution, parse_window


def test_parse_window_maps_outcomes_and_tokens():
    row = {
        "slug": "btc-updown-5m-100",
        "conditionId": "0xabc",
        "question": "BTC",
        "eventStartTime": "2026-11-24T00:00:00Z",
        "endDate": "2026-11-24T00:05:00Z",
        "clobTokenIds": json.dumps(["up-token", "down-token"]),
        "outcomes": json.dumps(["Up", "Down"]),
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "feeSchedule": {"rate": 0.05},
    }
    w = parse_window(row, 100)
    assert w.up_token == "up-token"
    assert w.down_token == "down-token"
    assert w.end_ts - w.start_ts == 300
    assert w.fee_rate == 0.05


def test_okx_cache_applies_snapshot_and_deletion():
    cache = OkxCache("BTC-USDT")
    cache.apply({"seqId": 1, "prevSeqId": -1, "bids": [["100", "2", "0", "1"]], "asks": [["101", "3", "0", "1"]]})
    cache.apply({"seqId": 2, "prevSeqId": 1, "bids": [["100", "0", "0", "0"], ["99", "4", "0", "1"]], "asks": []})
    book = cache.snapshot(1, 20)
    assert [level.price for level in book.bids] == [99]
    assert [level.price for level in book.asks] == [101]


def test_okx_cache_flags_a_sequence_gap():
    cache = OkxCache("BTC-USDT")
    cache.apply({"seqId": 1, "prevSeqId": -1, "bids": [["100", "2", "0", "1"]], "asks": []})
    cache.apply({"seqId": 5, "prevSeqId": 4, "bids": [["101", "2", "0", "1"]], "asks": []})
    assert cache.gapped is True
    assert cache.bids == {}


def test_parse_levels_drops_zero_sizes_and_sorts():
    levels = parse_levels([{"price": "0.5", "size": "10"}, {"price": "0.4", "size": "0"}])
    assert levels == (Level(0.5, 10.0),)
    assert [x.price for x in top(levels + (Level(0.6, 1),), 5, True)] == [0.6, 0.5]


def test_poly_trade_has_stable_id():
    window = parse_window(
        {
            "slug": "btc-updown-5m-100",
            "conditionId": "0xabc",
            "clobTokenIds": json.dumps(["up", "down"]),
            "outcomes": json.dumps(["Up", "Down"]),
            "eventStartTime": "2026-11-24T00:00:00Z",
            "endDate": "2026-11-24T00:05:00Z",
        },
        100,
    )
    row = {"timestamp": 100, "price": 0.5, "size": 5, "token_id": "up", "side": "BUY", "transaction_hash": "0xdead"}
    a = _parse_poly_trade(row, window)
    b = _parse_poly_trade(row, window)
    assert a is not None and a.trade_id == b.trade_id


def test_resolution_ignores_open_market_prices():
    window = parse_window(
        {
            "slug": "btc-updown-5m-100",
            "conditionId": "0xabc",
            "clobTokenIds": json.dumps(["up", "down"]),
            "outcomes": json.dumps(["Up", "Down"]),
            "eventStartTime": "2026-11-24T00:00:00Z",
            "endDate": "2026-11-24T00:05:00Z",
        },
        100,
    )
    open_row = {"closed": False, "outcomePrices": json.dumps(["0.2", "0.8"])}
    assert _parse_resolution(open_row, window) is None
    closed_row = {"closed": True, "outcomePrices": json.dumps(["1", "0"])}
    resolved = _parse_resolution(closed_row, window)
    assert resolved is not None and resolved.outcome == "Up"


def test_parse_clob_resolution_uses_winner_flags():
    window = parse_window(
        {
            "slug": "btc-updown-5m-100",
            "conditionId": "0xabc",
            "clobTokenIds": json.dumps(["up", "down"]),
            "outcomes": json.dumps(["Up", "Down"]),
            "eventStartTime": "2026-11-24T00:00:00Z",
            "endDate": "2026-11-24T00:05:00Z",
        },
        100,
    )
    row = {
        "closed": True,
        "tokens": [
            {"outcome": "Up", "price": 0.0, "winner": False},
            {"outcome": "Down", "price": 1.0, "winner": True},
        ],
    }
    resolved = _parse_clob_resolution(row, window)
    assert resolved is not None
    assert resolved.outcome == "Down"
    assert resolved.down_price == 1.0
