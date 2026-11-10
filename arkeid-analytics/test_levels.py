"""Оценка показателей относительно распределения аккаунта."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web.queries import _terciles, level_of


def main() -> int:
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    def basic():
        b = _terciles([1, 2, 3, 4, 5, 6, 7, 8, 9])
        assert b, "границы не посчитаны"
        assert level_of(9, b) == "good"
        assert level_of(1, b) == "weak"
        assert level_of(5, b) == "mid"
    expect("трети делят распределение", basic)

    def integers_keep_a_middle():
        # ровно тот случай, на котором ломалось отношение к медиане:
        # сохранения — мелкие целые, медиана 1.5, среднего разряда не было
        saves = [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 5, 7]
        b = _terciles(saves)
        levels = [level_of(v, b) for v in saves]
        assert "mid" in levels, "у целых счётчиков пропал средний разряд"
        assert "good" in levels and "weak" in levels
    expect("на мелких целых остаётся средний разряд", integers_keep_a_middle)

    def ties_are_middle():
        b = _terciles([5, 5, 5, 5, 5, 5, 5, 5, 9])
        # низ и верх совпали — разброса нет, красить нечего
        assert b is None or level_of(5, b) == "mid"
    expect("равные значения не красятся", ties_are_middle)

    def no_spread():
        assert _terciles([3] * 10) is None, "при нулевом разбросе границ быть не должно"
        assert level_of(3, None) == ""
    expect("нет разброса — нет оценки", no_spread)

    def too_few():
        assert _terciles([1, 2, 3]) is None, "на трёх роликах делить на трети нельзя"
    expect("на малой выборке оценка не выдаётся", too_few)

    def missing_value():
        assert level_of(None, (1, 9)) == ""
    expect("отсутствующее значение не красится", missing_value)

    print(f"\n{'все тесты прошли' if not failures else str(len(failures)) + ' упало'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
