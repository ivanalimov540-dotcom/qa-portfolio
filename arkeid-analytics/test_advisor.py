"""Советчик обязан быть честным насчёт объёма выборки."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import advisor as a


def reel(i, hook, saved=50, mult=1.0, hook_score=0.6, duration=30.0):
    return {"id": i, "hook_text": hook, "saved": saved, "multiplier": mult,
            "hook": hook_score, "duration_sec": duration}


def main() -> int:
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    def small_sample_is_flagged():
        items = [reel(1, "Сколько стоит?", saved=200), reel(2, "Просто текст", saved=50),
                 reel(3, "Ещё текст", saved=50)]
        f = a.compare(items, "вопрос", "", a.has_question, "saved")
        assert f["status"] == "insufficient", f"статус {f['status']}, ожидался insufficient"
    expect("маленькая группа помечается, а не выдаёт вывод", small_sample_is_flagged)

    def hypothesis_vs_confirmed():
        # 4 против 4 — гипотеза
        items = [reel(i, "Сколько это стоит?", saved=200) for i in range(4)] + \
                [reel(i + 10, "Обычный хук", saved=50) for i in range(4)]
        assert a.compare(items, "в", "", a.has_question, "saved")["status"] == "hypothesis"
        # 10 против 10 — подтверждено
        items = [reel(i, "Сколько это стоит?", saved=200) for i in range(10)] + \
                [reel(i + 20, "Обычный хук", saved=50) for i in range(10)]
        assert a.compare(items, "в", "", a.has_question, "saved")["status"] == "confirmed"
    expect("вывод повышается до подтверждённого только на объёме", hypothesis_vs_confirmed)

    def noise_is_not_a_finding():
        items = [reel(i, "Сколько стоит?", saved=52) for i in range(10)] + \
                [reel(i + 20, "Обычный хук", saved=50) for i in range(10)]
        f = a.compare(items, "в", "", a.has_question, "saved")
        assert f["status"] == "noise", f"разница 4% не должна считаться сигналом, статус {f['status']}"
    expect("мелкая разница называется шумом", noise_is_not_a_finding)

    def lift_is_correct():
        items = [reel(i, "Сколько стоит?", saved=150) for i in range(10)] + \
                [reel(i + 20, "Обычный хук", saved=100) for i in range(10)]
        f = a.compare(items, "в", "", a.has_question, "saved")
        assert abs(f["lift"] - 0.5) < 1e-9, f"lift {f['lift']}, ожидалось 0.5"
    expect("разница считается верно", lift_is_correct)

    def traits():
        assert a.has_number({"hook_text": "50 млн надо?"})
        assert not a.has_number({"hook_text": "Просто текст"})
        assert a.is_warning({"hook_text": "Не делайте так со своим салоном"})
        assert a.is_warning({"hook_text": "Почему нельзя мыть машину средством для посуды"})
        assert not a.is_warning({"hook_text": "Полировка фар: до и после"})
        assert a.short_hook({"hook_text": "Это не новая машина"})
        assert not a.short_hook({"hook_text": " ".join(["слово"] * 12)})
    expect("признаки хука определяются верно", traits)

    def music_only_reels_are_excluded():
        # у роликов без речи хука нет — они не должны влиять на выводы
        items = [reel(i, "Сколько это стоит?", saved=200) for i in range(10)] + \
                [reel(i + 20, "Обычный хук", saved=50) for i in range(10)] + \
                [reel(i + 40, "", saved=999) for i in range(10)]
        s = a.summary(items)
        assert s["with_transcript"] == 20, f"учтено {s['with_transcript']}, ожидалось 20"
        best = a.worth_repeating(items)
        assert all(r["hook_text"] for r in best), "в «что повторить» попал ролик без речи"
    expect("ролики без речи не участвуют в выводах", music_only_reels_are_excluded)

    def summary_honesty():
        # «Достаточно» — это не количество роликов, а наличие подтверждённого вывода.
        # Сорок одинаковых роликов не дают ни одного, сколько бы их ни было.
        same = [reel(i, "одинаковый хук") for i in range(40)]
        s = a.summary(same)
        assert s["with_transcript"] == 40
        assert not s["enough"], "сорок одинаковых роликов не образуют закономерности"

        # А вот явное различие на достаточных группах — образует
        split = [reel(i, "Сколько это стоит?", saved=200) for i in range(10)] + \
                [reel(i + 50, "Обычный хук", saved=50) for i in range(10)]
        s2 = a.summary(split)
        assert s2["enough"], "чёткое различие на 10 против 10 должно считаться достаточным"
        assert s2["confirmed"] >= 1
    expect("достаточность выборки определяется выводами, а не числом роликов", summary_honesty)

    def music_only_not_counted_as_group():
        # ролик без речи не «не содержит вопроса» — у него хука нет вовсе
        items = [reel(i, "Сколько стоит?", saved=200) for i in range(4)] + \
                [reel(i + 20, "Обычный хук", saved=50) for i in range(4)] + \
                [reel(i + 40, "", saved=1) for i in range(40)]
        f = a.compare(a.with_hooks(items), "в", "", a.has_question, "saved")
        assert f["n_with"] + f["n_without"] == 8, \
            f"в сравнение попало {f['n_with'] + f['n_without']} роликов вместо 8"
    expect("ролики без хука не попадают в группу сравнения", music_only_not_counted_as_group)

    print(f"\n{'все тесты прошли' if not failures else str(len(failures)) + ' упало'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
