"""Произвольный период: границы, подписи и защита от мусора в адресе."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import queries as q


def main() -> int:
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    today = datetime.now(timezone.utc).date()

    def presets_unchanged():
        """Пресеты должны считать ровно то же, что и до появления диапазонов."""
        for key, days in q.PERIODS.items():
            p = q.resolve_period(key)
            assert p.key == key
            assert p.end == today, f"{key}: конец не сегодня"
            assert p.start == today - timedelta(days=days), f"{key}: начало съехало"
    expect("пресеты считают то же, что раньше", presets_unchanged)

    def custom_bounds():
        p = q.resolve_period("custom", "2026-08-01", "2026-08-31")
        assert p.custom
        assert p.start == date(2026, 8, 1) and p.end == date(2026, 8, 31)
        assert p.span == 31, f"длина {p.span}"
    expect("свой диапазон берёт обе границы включительно", custom_bounds)

    def reversed_dates_are_swapped():
        p = q.resolve_period("custom", "2026-08-31", "2026-08-01")
        assert p.start == date(2026, 8, 1) and p.end == date(2026, 8, 31)
    expect("перепутанные местами даты меняются обратно", reversed_dates_are_swapped)

    def garbage_falls_back():
        for bad in ("ерунда", "", "2026-13-40", "'; DROP TABLE media; --"):
            p = q.resolve_period("custom", bad, bad)
            assert not p.custom, f"мусор {bad!r} прошёл как диапазон"
            assert p.key == "month"
    expect("мусор в адресе откатывается к пресету", garbage_falls_back)

    def future_end_is_clamped():
        p = q.resolve_period("custom", today.isoformat(), (today + timedelta(days=90)).isoformat())
        assert p.end == today, "конец уехал в будущее"
    expect("конец не уходит в будущее", future_end_is_clamped)

    def huge_range_is_capped():
        p = q.resolve_period("custom", "1970-01-01", today.isoformat())
        assert p.span <= q.MAX_CUSTOM_DAYS, f"диапазон {p.span} дней не ограничен"
    expect("гигантский диапазон обрезается", huge_range_is_capped)

    def previous_does_not_overlap():
        p = q.resolve_period("custom", "2026-08-10", "2026-08-20")
        prev = p.previous()
        assert prev.end < p.start, "прошлый период налезает на текущий"
        assert prev.span == p.span, f"длина не совпала: {prev.span} против {p.span}"
        assert prev.end == p.start - timedelta(days=1), "между периодами разрыв"
    expect("прошлый период той же длины и вплотную", previous_does_not_overlap)

    def labels_read_like_russian():
        cases = [
            (("2026-09-01", "2026-09-14"), "с 1 по 14 сентября"),
            (("2026-08-20", "2026-09-07"), "с 20 августа по 7 сентября"),
            (("2026-09-05", "2026-09-05"), "за 5 сентября"),
        ]
        for (a, b), want in cases:
            got = q.resolve_period("custom", a, b).label
            assert got == want, f"{a}..{b} → {got!r}, ожидалось {want!r}"
    expect("подпись периода читается по-русски", labels_read_like_russian)

    def cross_year_label_has_years():
        label = q.resolve_period("custom", "2025-12-20", "2026-01-10").label
        assert "2025" in label and "2026" in label, f"годы потерялись: {label}"
    expect("диапазон через новый год показывает годы", cross_year_label_has_years)

    def query_args_round_trip():
        p = q.resolve_period("custom", "2026-08-01", "2026-08-31")
        args = p.query_args()
        again = q.resolve_period(args["period"], args["from"], args["to"])
        assert again.start == p.start and again.end == p.end, "период теряется при переходе"
        assert q.resolve_period("week").query_args() == {"period": "week"}
    expect("период переживает переход по ссылке", query_args_round_trip)

    def granularity_matches_span():
        assert "day" in q.granularities_for(q.resolve_period("custom", "2026-09-01", "2026-09-07"))
        long_one = q.granularities_for(q.resolve_period("custom", "2026-01-01", "2026-06-30"))
        assert "month" in long_one, f"на полугодии нет разреза по месяцам: {long_one}"
        assert "day" not in long_one, f"на полугодии дневные столбики нечитаемы: {long_one}"
    expect("разрез графика подбирается по длине", granularity_matches_span)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
