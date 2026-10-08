"""thrift_api.deadlines (WO33): ship-by dates by the marketplace's rule from the sale's local date, the observed US
federal holidays, reminders at 09:00 local on both sides of the DST changes, quiet hours, the day's short form."""
from datetime import date, datetime, timedelta, timezone

import pytest

from thrift_api.deadlines import (DEFAULT_RULES, add_business_days, fmt_day, is_business_day, local, quiet,
                                  reminders_due, rules, ship_by, us_federal_holidays)

UTC = timezone.utc


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def sale(sale_id: str, ship_by: date | str, shipped_at: str | None = None, status: str = "sold") -> dict:
    return {"id": sale_id, "ship_by": ship_by, "shipped_at": shipped_at, "status": status}


# --- holidays and business days

def test_holidays_2026_observed():
    assert us_federal_holidays(2026) == {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19),
        date(2026, 7, 3),                   # Jul 4 is a Saturday: the Friday before
        date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25)}


def test_holidays_2027_saturday_christmas_and_next_new_year():
    assert us_federal_holidays(2027) == {
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 5, 31),
        date(2027, 6, 18),                  # Jun 19 is a Saturday
        date(2027, 7, 5),                   # Jul 4 is a Sunday: the Monday after
        date(2027, 9, 6), date(2027, 10, 11), date(2027, 11, 11), date(2027, 11, 25),
        date(2027, 12, 24),                 # Dec 25 is a Saturday
        date(2027, 12, 31)}                 # Jan 1 2028 is a Saturday: in 2027's set
    in_2028 = us_federal_holidays(2028)
    assert date(2028, 1, 1) not in in_2028 and date(2027, 12, 31) not in in_2028
    assert date(2028, 11, 10) in in_2028    # Veterans Day on a Saturday
    assert len(in_2028) == 10
    assert not is_business_day(date(2027, 12, 24)) and not is_business_day(date(2027, 12, 31))
    assert is_business_day(date(2027, 12, 30)) and is_business_day(date(2028, 1, 3))
    assert not is_business_day(date(2028, 1, 1))    # a Saturday anyway


def test_the_holiday_set_is_a_copy():
    us_federal_holidays(2026).clear()
    assert not is_business_day(date(2026, 7, 3))


def test_business_days_skip_weekends_and_holidays_never_counting_the_start():
    assert add_business_days(date(2026, 9, 16), 5) == date(2026, 9, 23)     # Wed -> the next Wed
    assert add_business_days(date(2026, 9, 18), 1) == date(2026, 9, 21)     # Fri -> Mon
    assert add_business_days(date(2026, 9, 19), 5) == date(2026, 9, 25)     # from a Saturday: Mon is the 1st
    assert add_business_days(date(2026, 9, 20), 1) == date(2026, 9, 21)     # from a Sunday
    assert add_business_days(date(2026, 11, 26), 1) == date(2026, 11, 27)   # from Thanksgiving
    assert add_business_days(date(2026, 9, 16), 0) == date(2026, 9, 16)
    with pytest.raises(ValueError):
        add_business_days(date(2026, 9, 16), -1)


# --- ship-by dates

def test_vinted_five_business_days_across_a_weekend():
    assert ship_by("vinted", utc(2026, 9, 16, 15, 0)) == (date(2026, 9, 23), "rule")


def test_vinted_across_columbus_day():
    # sold Thu Oct 8: Fri 9, (weekend, Mon 12 Columbus Day), Tue 13, Wed 14, Thu 15, Fri 16
    assert ship_by("vinted", utc(2026, 10, 8, 15, 0)) == (date(2026, 10, 16), "rule")


def test_vinted_across_thanksgiving():
    # sold Fri Nov 20: Mon 23, Tue 24, Wed 25, (Thu 26 Thanksgiving), Fri 27, Mon 30
    assert ship_by("vinted", utc(2026, 11, 20, 18, 0)) == (date(2026, 11, 30), "rule")


def test_vinted_across_christmas_and_new_year_observed():
    # sold Thu Dec 23 2027: (Fri 24 Christmas observed, weekend), Mon 27 .. Thu 30, (Fri 31 New Year's 2028 observed,
    # weekend), Mon Jan 3
    assert ship_by("vinted", utc(2027, 12, 23, 17, 0)) == (date(2028, 1, 3), "rule")


def test_poshmark_seven_and_depop_five_calendar_days():
    sold = utc(2026, 10, 8, 15, 0)      # Thu Oct 8, 11:00 local
    assert ship_by("poshmark", sold) == (date(2026, 10, 15), "rule")
    assert ship_by("depop", sold) == (date(2026, 10, 13), "rule")     # the weekend and Columbus Day count
    assert ship_by("Poshmark", sold) == (date(2026, 10, 15), "rule")


def test_a_late_sale_counts_from_the_local_day_not_the_utc_one():
    assert local(utc(2026, 10, 8, 3, 30)).isoformat() == "2026-10-07T23:30:00-04:00"
    assert ship_by("poshmark", utc(2026, 10, 8, 3, 30)) == (date(2026, 10, 14), "rule")   # Oct 7 local
    assert ship_by("poshmark", utc(2026, 10, 8, 4, 30)) == (date(2026, 10, 15), "rule")   # 00:30 on Oct 8 local
    assert ship_by("poshmark", utc(2026, 12, 2, 4, 30)) == (date(2026, 12, 8), "rule")    # 23:30 EST on Dec 1
    assert ship_by("poshmark", utc(2026, 12, 2, 5, 0)) == (date(2026, 12, 9), "rule")     # midnight EST


def test_local_takes_a_naive_time_as_utc():
    assert local(datetime(2026, 10, 8, 3, 30)) == local(utc(2026, 10, 8, 3, 30))
    assert local(datetime(2026, 10, 8, 3, 30)).utcoffset() == timedelta(hours=-4)


def test_a_stated_date_wins():
    assert ship_by("vinted", utc(2026, 10, 8, 15, 0), stated=date(2026, 10, 20)) == (date(2026, 10, 20), "email")
    assert ship_by("poshmark", utc(2026, 10, 8, 15, 0), stated=date(2026, 10, 10)) == (date(2026, 10, 10), "email")


def test_an_unknown_marketplace_is_an_error():
    with pytest.raises(ValueError):
        ship_by("ebay", utc(2026, 10, 8, 15, 0))
    with pytest.raises(ValueError):
        ship_by("ebay", utc(2026, 10, 8, 15, 0), stated=date(2026, 10, 20))


def test_rules_merge_overrides():
    merged = rules({"vinted": {"days": 3}, "depop": {"kind": "business"}, "ebay": {"kind": "calendar", "days": 3},
                    "poshmark": None})
    assert merged == {"poshmark": {"kind": "calendar", "days": 7}, "vinted": {"kind": "business", "days": 3},
                      "depop": {"kind": "business", "days": 5}, "ebay": {"kind": "calendar", "days": 3}}
    assert rules() == DEFAULT_RULES and rules() is not DEFAULT_RULES
    assert DEFAULT_RULES["vinted"] == {"kind": "business", "days": 5}     # untouched
    cfg = {"depop": {"kind": "business", "days": 3}}
    # sold Thu Oct 8: Fri 9, (weekend, Columbus Day), Tue 13, Wed 14
    assert ship_by("depop", utc(2026, 10, 8, 15, 0), cfg=cfg) == (date(2026, 10, 14), "rule")


@pytest.mark.parametrize("bad", [{"vinted": {"kind": "weekly"}}, {"vinted": {"days": -1}}, {"vinted": {"days": "five"}},
                                 {"ebay": {"days": 3}}, {"vinted": 5}])
def test_rules_refuse_a_bad_override(bad):
    with pytest.raises(ValueError):
        rules(bad)


# --- reminders

# 09:00 local in UTC: EST (UTC-5) until Sun Mar 8 2026 02:00 and again from Sun Nov 1 2026 02:00, EDT (UTC-4) between
@pytest.mark.parametrize("day, nine_utc", [(date(2026, 3, 7), 14), (date(2026, 3, 8), 13), (date(2026, 3, 9), 13),
                                           (date(2026, 10, 31), 13), (date(2026, 11, 1), 14), (date(2026, 11, 2), 14)])
def test_reminders_at_nine_local_across_dst(day, nine_utc):
    sales = [sale("tomorrow", (day + timedelta(days=1)).isoformat()), sale("today", day.isoformat()),
             sale("yesterday", (day - timedelta(days=1)).isoformat())]
    nine = utc(day.year, day.month, day.day, nine_utc, 0)
    assert reminders_due(nine - timedelta(minutes=1), sales, set()) == []
    assert reminders_due(nine, sales, set()) == [("tomorrow", "day_before"), ("today", "due_today"),
                                                 ("yesterday", "overdue")]


def test_each_reminder_once_over_a_week_of_ticks():
    sales = [sale("a", "2026-10-09")]
    sent, log = set(), []
    t = utc(2026, 10, 7, 0, 0)
    while t < utc(2026, 10, 14, 0, 0):
        for due in reminders_due(t, sales, sent):
            sent.add(due)
            log.append((local(t).strftime("%m-%d %H:%M"), due[1]))
        t += timedelta(minutes=30)
    assert log == [("10-08 09:00", "day_before"), ("10-09 09:00", "due_today"), ("10-10 09:00", "overdue")]


def test_never_twice():
    sales = [sale("a", "2026-10-09"), sale("b", "2026-10-09")]
    now = utc(2026, 10, 9, 14, 0)       # 10:00 local on the due day
    assert reminders_due(now, sales, set()) == [("a", "due_today"), ("b", "due_today")]
    assert reminders_due(now, sales, {("a", "due_today")}) == [("b", "due_today")]
    assert reminders_due(now, sales, {("a", "day_before"), ("b", "due_today")}) == [("a", "due_today")]


def test_overdue_from_the_day_after_and_a_missed_day_not_made_up():
    sales = [sale("a", date(2026, 10, 9))]      # a date, not a string
    assert reminders_due(utc(2026, 10, 7, 20, 0), sales, set()) == []                     # two days before
    assert reminders_due(utc(2026, 10, 8, 12, 59), sales, set()) == []                    # 08:59 the day before
    assert reminders_due(utc(2026, 10, 9, 20, 0), sales, set()) == [("a", "due_today")]   # the day before was missed
    assert reminders_due(utc(2026, 10, 10, 13, 0), sales, set()) == [("a", "overdue")]
    assert reminders_due(utc(2026, 10, 12, 12, 59), sales, set()) == []                   # 08:59 a later day
    assert reminders_due(utc(2026, 10, 12, 13, 0), sales, set()) == [("a", "overdue")]    # still not sent
    assert reminders_due(utc(2026, 10, 12, 13, 0), sales, {("a", "overdue")}) == []
    sent = {("a", "due_today")}
    assert reminders_due(utc(2026, 10, 9, 20, 0), sales, sent) == []                      # never day_before instead


def test_no_reminder_for_a_shipped_or_closed_sale():
    sales = [sale("shipped", "2026-10-09", shipped_at="2026-10-09T12:00:00+00:00"),
             *(sale(status, "2026-10-09", status=status) for status in ("cancelled", "double_sale", "done")),
             sale("no date", None),
             sale("open", "2026-10-09")]
    assert reminders_due(utc(2026, 10, 9, 14, 0), sales, set()) == [("open", "due_today")]
    assert reminders_due(utc(2026, 10, 12, 14, 0), sales[:-1], set()) == []


def test_reminder_time_and_zone_are_settings():
    sales = [sale("a", "2026-10-09")]
    assert reminders_due(utc(2026, 10, 9, 22, 29), sales, set(), at="18:30") == []
    assert reminders_due(utc(2026, 10, 9, 22, 30), sales, set(), at="18:30") == [("a", "due_today")]
    assert reminders_due(utc(2026, 10, 9, 15, 59), sales, set(), tz="America/Los_Angeles") == []
    assert reminders_due(utc(2026, 10, 9, 16, 0), sales, set(), tz="America/Los_Angeles") == [("a", "due_today")]
    with pytest.raises(ValueError):
        reminders_due(utc(2026, 10, 9, 16, 0), sales, set(), at="9am")


# --- quiet hours, the day's short form

@pytest.mark.parametrize("now, expected", [
    (utc(2026, 10, 8, 2, 59), False),       # 22:59 EDT on Oct 7
    (utc(2026, 10, 8, 3, 0), True),         # 23:00
    (utc(2026, 10, 8, 11, 59), True),       # 07:59 on Oct 8
    (utc(2026, 10, 8, 12, 0), False),       # 08:00
    (utc(2026, 12, 2, 3, 59), False),       # 22:59 EST on Dec 1
    (utc(2026, 12, 2, 4, 0), True),         # 23:00 EST
    (utc(2026, 12, 2, 12, 59), True),       # 07:59 EST on Dec 2
    (utc(2026, 12, 2, 13, 0), False),       # 08:00 EST
])
def test_quiet_hours(now, expected):
    assert quiet(now) is expected


def test_quiet_window_within_one_day():
    assert not quiet(utc(2026, 10, 8, 16, 59), start="13:00", end="14:00")     # 12:59 local
    assert quiet(utc(2026, 10, 8, 17, 30), start="13:00", end="14:00")
    assert not quiet(utc(2026, 10, 8, 18, 0), start="13:00", end="14:00")


def test_fmt_day():
    assert fmt_day(date(2025, 10, 9)) == "Thu Oct 9"
    assert fmt_day(date(2026, 10, 9)) == "Fri Oct 9"
    assert fmt_day(date(2026, 3, 1)) == "Sun Mar 1"
    assert fmt_day(date(2027, 12, 31)) == "Fri Dec 31"
