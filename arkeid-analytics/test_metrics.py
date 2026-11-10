"""Проверка формул из ТЗ §6."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import metrics as m


def approx(a, b, eps=1e-9):
    assert a is not None, "ожидалось число, получен None"
    assert abs(a - b) < eps, f"{a} != {b}"


def test_er():
    approx(m.er_by_reach(500, 10_000), 0.05)
    approx(m.er_by_followers(200, 4_000), 0.05)
    assert m.er_by_reach(500, 0) is None, "деление на ноль должно давать None"
    assert m.er_by_reach(None, 100) is None


def test_interactions():
    assert m.interactions(likes=10, comments=2, saved=5, shares=1) == 18
    assert m.interactions(likes=None, comments=3) == 3


def test_retention_and_hook():
    approx(m.retention(9.0, 30.0), 0.3)
    approx(m.hook_score(0.42), 0.58)
    assert m.hook_score(None) is None


def test_outlier_uses_median_not_mean():
    # Один выброс не должен ломать базу сравнения.
    values = [1000, 1100, 900, 1050, 143_000]
    med = m.median(values)
    approx(med, 1050)
    approx(m.outlier_multiplier(143_000, med), 136.19047619047618, eps=1e-6)
    # Для обычного ролика множитель остаётся близким к единице.
    approx(m.outlier_multiplier(1000, med), 0.9523809523809523, eps=1e-9)


def test_velocity():
    approx(m.velocity_per_hour(1000, 2200, 24), 50.0)
    assert m.velocity_per_hour(1000, 2200, 0) is None


def test_posts_per_week():
    stamps = [datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 15, tzinfo=timezone.utc)]
    approx(m.posts_per_week(stamps), 1.0)
    assert m.posts_per_week(stamps[:1]) is None


def test_format_mix():
    mix = m.format_mix(["REELS"] * 96 + ["FEED"] * 4)
    approx(mix["REELS"], 0.96)
    approx(mix["FEED"], 0.04)
    assert m.format_mix([]) == {}


def test_contact_taps():
    breakdown = {"text_message_clicks": 4, "phone_call_clicks": 3, "get_directions_clicks": 5, "website_clicks": 99}
    # website_clicks заявкой не считается
    assert m.contact_taps_total(breakdown) == 12
    assert m.contact_taps_total({}) == 0


def test_taps_per_1k():
    approx(m.taps_per_1k_reach(12, 6000), 2.0)


def test_viral_share():
    # медиана 1000; два поста от 2000 и выше
    assert m.viral_share([500, 1000, 1000, 2000, 5000]) == 0.4


def test_weight_class():
    assert m.weight_class(3_000) == "до 10K"
    assert m.weight_class(10_000) == "до 50K"
    assert m.weight_class(180_000) == "до 200K"
    assert m.weight_class(1_200_000) == "200K+"
    assert m.weight_class(None) == "неизвестно"


def test_parse_ts():
    assert m.parse_ts("2026-08-22T15:04:05+0000").year == 2026
    assert m.parse_ts(None) is None
    assert m.parse_ts("мусор") is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL {name}: {exc}")
    print(f"\n{'все тесты прошли' if not failed else str(failed) + ' упало'}")
    sys.exit(1 if failed else 0)
