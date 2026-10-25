from maxxing.eval import ScenarioResult, parse_ts, scorecard


def test_parse_ts_formats():
    assert parse_ts("2026-10-25T19:17:30Z") == parse_ts(1792955850) == parse_ts(1792955850000)
    assert parse_ts(None) is None and parse_ts("garbage") is None


def test_scoring():
    planted = {"id": "2-null-deref", "expected_sha": "abc123", "expected_category": "code", "expect_incident": True}
    assert ScenarioResult("2", planted, detected=True, culprit="abc123ff" * 5, category="code").passed
    assert not ScenarioResult("2", planted, detected=True, culprit="ffff0000", category="code").passed
    assert not ScenarioResult("2", planted, detected=True, culprit="abc123ff", category="resource").passed
    assert not ScenarioResult("2", planted, detected=False).passed

    infra = {"id": "5-redis-down", "expected_sha": None, "expected_category": "dependency", "expect_incident": True}
    assert ScenarioResult("5", infra, detected=True, culprit=None, category="dependency").passed
    assert not ScenarioResult("5", infra, detected=True, culprit="abc", category="dependency").passed  # blamed an innocent

    surge = {"id": "6-healthy-surge", "expected_sha": None, "expected_category": None, "expect_incident": False}
    assert ScenarioResult("6", surge, detected=False).passed
    assert not ScenarioResult("6", surge, detected=True, category="resource").passed

    table = scorecard([ScenarioResult("6", surge, detected=False)])
    assert table.caption == "1/1 scenarios passed"
