"""Date preference resolution: the phrases the phase spec calls out."""

from __future__ import annotations

from datetime import date, timedelta

from app.services.date_parsing import resolve_preferences, resolve_time_window
from app.services.date_phrases import resolve_phrase

# 2026-10-07 is a Wednesday; 2026-10-09 a Friday.
WED = date(2026, 10, 7)
FRI = date(2026, 10, 9)
SAT = date(2026, 10, 10)


def _days(phrase: str, today: date) -> list[date]:
    return resolve_phrase(phrase, today).days


def test_tomorrow_from_wednesday():
    assert _days("tomorrow", WED) == [date(2026, 10, 8)]


def test_next_tuesday_from_wednesday_is_six_days_out():
    # Most callers mean the next Tuesday that has not happened yet.
    assert _days("next Tuesday", WED) == [date(2026, 10, 13)]


def test_next_tuesday_at_the_weekend_is_imminent():
    # On a Saturday "next Tuesday" is three days out; no reading is plausible
    # enough to need clarification (unlike "next Monday", which is flagged).
    result = resolve_phrase("next tuesday", SAT)
    assert result.days == [date(2026, 10, 13)]
    assert result.ambiguous is False

    monday = resolve_phrase("next monday", SAT)
    # On a Saturday, "next Monday" is two days away to most people and nine to
    # others, so it is flagged and the assistant confirms instead of guessing.
    assert monday.ambiguous is True
    assert monday.days == [date(2026, 10, 12)]


def test_this_coming_friday_is_this_week():
    assert _days("this coming Friday", WED) == [date(2026, 10, 9)]


def test_bare_weekday_that_already_passed_rolls_to_next_week():
    assert _days("monday", WED) == [date(2026, 10, 12)]


def test_bare_weekday_equal_to_today_is_flagged():
    result = resolve_phrase("Wednesday", WED)
    assert result.ambiguous is True
    assert result.days == [WED]


def test_later_next_week_starts_thursday():
    days = _days("later next week", WED)
    assert days[0] == date(2026, 10, 15)
    assert all(day.weekday() in {3, 4, 5} for day in days)


def test_next_week_is_next_monday_to_saturday():
    days = _days("next week", WED)
    assert days[0] == date(2026, 10, 12)
    assert days[-1] == date(2026, 10, 17)


def test_early_next_month_resolves_within_first_ten_days():
    days = _days("early next month", WED)
    assert days[0].month == 11
    assert max(day.day for day in days) <= 10
    assert all(day >= WED for day in days)


def test_next_sunday_is_closed_not_ambiguous():
    days = _days("next Sunday", WED)
    assert days == [date(2026, 10, 11)]
    assert days[0].weekday() == 6


def test_stated_year_is_taken_literally():
    # A past year is past, not silently rolled to the next year.
    past = resolve_phrase("march 5 2026", WED)
    assert past.past is True and past.days == []
    assert resolve_phrase("march 5 2027", WED).days == [date(2027, 3, 5)]
    # Without a year, the next occurrence is used.
    assert resolve_phrase("march 5", WED).days == [date(2027, 3, 5)]


def test_iso_and_slash_dates():
    assert _days("2026-11-02", WED) == [date(2026, 11, 2)]
    assert _days("11/2", WED) == [date(2026, 11, 2)]


def test_day_month_order_is_inferred_when_unambiguous():
    # 25 cannot be a month, so 25/12 must be read day/month.
    assert _days("25/12", WED) == [date(2026, 12, 25)]


def test_genuinely_ambiguous_slash_date_is_flagged():
    result = resolve_phrase("03/04", WED)
    assert result.ambiguous is True
    assert result.days == [date(2027, 3, 4)]


def test_ordinal_only_day_is_ambiguous():
    result = resolve_phrase("the 12th", WED)
    assert result.ambiguous is True
    assert result.days == []


def test_past_date_is_rejected_not_rolled_forward():
    result = resolve_phrase("2026-01-05", WED)
    assert result.past is True
    assert result.days == []
    assert result.clarification


def test_impossible_date_is_reported():
    result = resolve_phrase("2026-02-30", WED)
    assert result.days == []
    assert result.unreadable or result.warnings


def test_any_day_except_wednesday_excludes_wednesday():
    resolved = resolve_preferences(exclude_weekdays=["Wednesday"], today=WED)
    assert resolved.weekday_filter is not None
    assert 2 not in resolved.weekday_filter
    assert resolved.accepts_day(date(2026, 10, 8)) is True
    assert resolved.accepts_day(date(2026, 10, 7)) is False


def test_weekends_only_never_includes_a_weekday():
    # "weekends only" is modelled as weekdays=["Saturday"] (Sunday is closed).
    named = resolve_preferences(weekdays=["Saturday"], range_days=21, today=WED)
    assert named.weekday_filter == {5}
    assert named.accepts_day(date(2026, 10, 9)) is False
    assert named.accepts_day(date(2026, 10, 10)) is True


def test_window_phrases():
    morning = resolve_time_window("morning only")
    assert morning is not None
    assert morning.start_minute == 8 * 60
    assert morning.end_minute == 12 * 60

    after_three = resolve_time_window("after 3 PM")
    assert after_three.start_minute == 15 * 60
    assert after_three.end_minute == 18 * 60

    before_lunch = resolve_time_window("before lunch")
    assert before_lunch.end_minute == 12 * 60

    clamped = resolve_time_window("after 7 PM")
    assert clamped is None  # clinic closes at 6 PM: no overlap, not a silent widen


def test_window_and_day_filters_combine():
    from datetime import datetime

    resolved = resolve_preferences(
        date_phrases=["this coming Friday"],
        time_windows=["after 3 PM"],
        today=WED,
    )
    assert resolved.days == [FRI]
    assert resolved.accepts_time(datetime(2026, 10, 9, 16, 0)) is True
    assert resolved.accepts_time(datetime(2026, 10, 9, 9, 0)) is False


def test_past_start_date_is_reported_but_search_stays_future():
    resolved = resolve_preferences(start_date=(WED - timedelta(days=3)).isoformat(), today=WED)
    assert resolved.past_rejected is True
    assert resolved.scan_start == WED


def test_unreadable_phrase_surfaces_a_warning():
    resolved = resolve_preferences(date_phrases=["sometime"], today=WED)
    assert resolved.days == []
    assert resolved.warnings
