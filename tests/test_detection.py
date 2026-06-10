from datetime import datetime, timezone


from cc_cred.detection import parse_reset_time, is_rate_limited_text, is_rate_limited


def test_parse_reset_time_with_timezone():
    text = "You've hit your limit · resets 8pm (America/Denver)"
    result = parse_reset_time(text)
    assert result is not None
    assert result.tzinfo == timezone.utc
    # 8pm MDT (UTC-6) = 02:00 UTC next day
    assert result.hour == 2


def test_parse_reset_time_with_minutes():
    text = "resets 8:30pm (UTC)"
    result = parse_reset_time(text)
    assert result is not None
    assert result.minute == 30


def test_parse_reset_time_no_timezone_falls_back_to_utc():
    text = "resets 6pm"
    result = parse_reset_time(text)
    assert result is not None
    # Should be UTC, hour 18 or next day if already past
    assert result.tzinfo == timezone.utc


def test_parse_reset_time_invalid_timezone_falls_back_to_utc():
    text = "resets 8pm (Not/ATimezone)"
    result = parse_reset_time(text)
    assert result is not None
    assert result.tzinfo == timezone.utc


def test_parse_reset_time_no_reset_phrase_returns_none():
    text = "Your usage allocation has been disabled by your admin"
    result = parse_reset_time(text)
    assert result is None


def test_parse_reset_time_empty_returns_none():
    assert parse_reset_time("") is None


def test_parse_reset_time_always_future():
    # Regardless of time, result should be in the future
    text = "resets 12am (UTC)"
    result = parse_reset_time(text)
    assert result is not None
    assert result > datetime.now(timezone.utc)


def test_is_rate_limited_text_hit_your_limit():
    assert is_rate_limited_text("You've hit your limit") is True


def test_is_rate_limited_text_usage_allocation():
    assert is_rate_limited_text("Your usage allocation has been disabled by your admin") is True


def test_is_rate_limited_text_case_insensitive():
    assert is_rate_limited_text("HIT YOUR LIMIT") is True


def test_is_rate_limited_text_clean_message():
    assert is_rate_limited_text("Task completed successfully.") is False


def test_is_rate_limited_api_error_status_429():
    class FakeResult:
        api_error_status = 429
        errors = None

    assert is_rate_limited(FakeResult()) is True


def test_is_rate_limited_errors_list():
    class FakeResult:
        api_error_status = None
        errors = ["rate_limit error occurred"]

    assert is_rate_limited(FakeResult()) is True


def test_is_rate_limited_last_assistant_text():
    class FakeResult:
        api_error_status = None
        errors = None

    assert is_rate_limited(FakeResult(), last_assistant_text="You've hit your limit") is True


def test_is_rate_limited_clean():
    class FakeResult:
        api_error_status = 200
        errors = []

    assert is_rate_limited(FakeResult(), last_assistant_text="All done.") is False
