"""AC17: Live Analysis enforces 3 calls/session and 50 calls/day (with date rollover)."""
from app.demo_limits import DAILY_LIMIT_MSG, SESSION_LIMIT_MSG, DailyDemoCounter


def test_demo_caps_session(tmp_path):
    """AC17a: Simulate 4 live analysis requests in a session. Assert the 4th is blocked."""
    counter = DailyDemoCounter(tmp_path / "daily_demo.json", daily_cap=50)
    session = {}

    for i in range(3):
        allowed, msg = counter.check_and_increment(session, session_cap=3)
        assert allowed is True and msg is None
        assert session["demo_calls_used"] == i + 1

    allowed, msg = counter.check_and_increment(session, session_cap=3)
    assert allowed is False
    assert msg == SESSION_LIMIT_MSG
    assert counter.get_count() == 3  # 4th blocked call did not increment daily count


def test_demo_caps_daily_and_date_reset(tmp_path):
    """AC17b: Set file-backed daily counter to 50. Next request is blocked; resets on date change."""
    current_day = ["2026-10-24"]
    counter = DailyDemoCounter(tmp_path / "daily_demo.json", daily_cap=50, today_fn=lambda: current_day[0])
    counter.set_count(50)

    session = {}
    allowed, msg = counter.check_and_increment(session, session_cap=3)
    assert allowed is False
    assert msg == DAILY_LIMIT_MSG
    assert session.get("demo_calls_used", 0) == 0

    # Roll over to next day -> resets automatically
    current_day[0] = "2026-10-25"
    allowed2, msg2 = counter.check_and_increment(session, session_cap=3)
    assert allowed2 is True and msg2 is None
    assert counter.get_count() == 1
    assert session["demo_calls_used"] == 1
