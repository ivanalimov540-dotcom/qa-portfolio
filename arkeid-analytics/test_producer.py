"""Рилс-продюсер: что считается трендом, а что случайностью."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import niches, producer


def reel(user, mult, text):
    return {"username": user, "multiplier": mult, "caption": text,
            "hook_text": "", "id": abs(hash((user, text))) % 10**6}


def main() -> int:
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    def one_viral_reel_is_not_a_trend():
        """Главное правило: у любого автора раз в полгода что-то выстреливает
        само. Один ролик трендом объявлять нельзя."""
        groups = producer.confirm([reel("a", 50.0, "Сколько стоит оклейка")])
        assert groups, "приём вообще не распознался"
        assert not groups[0]["confirmed"], "один ролик объявлен трендом"
        assert groups[0]["missing"], "не сказано, чего не хватает"
    expect("один залетевший ролик трендом не считается", one_viral_reel_is_not_a_trend)

    def one_author_many_reels_is_not_a_trend():
        """Пять роликов одного автора — это его приём, а не тренд ниши."""
        items = [reel("a", 4.0, f"Сколько стоит керамика {i}") for i in range(6)]
        g = producer.confirm(items)[0]
        assert not g["confirmed"], "один автор объявлен трендом"
        assert any("автор" in x for x in g["missing"]), g["missing"]
    expect("много роликов одного автора — не тренд", one_author_many_reels_is_not_a_trend)

    def independent_authors_confirm():
        items = [reel(u, 3.0, f"Сколько стоит {u}") for u in ("a", "b", "c")]
        items.append(reel("a", 2.5, "Сколько стоит ещё"))
        g = producer.confirm(items)[0]
        assert g["confirmed"], f"не подтвердилось: {g['missing']}"
        assert g["authors"] == 3 and g["posts"] == 4, (g["authors"], g["posts"])
    expect("три независимых автора подтверждают приём", independent_authors_confirm)

    def weak_results_do_not_confirm():
        """Приём, который сняли многие и ни у кого не выстрелило, — не тренд."""
        items = [reel(u, 2.1, f"Сколько стоит {u}") for u in ("a", "b", "c", "d")]
        g = producer.confirm(items)[0]
        assert not g["confirmed"], "слабый результат объявлен трендом"
        assert any("множител" in x for x in g["missing"]), g["missing"]
    expect("слабый результат не подтверждает приём", weak_results_do_not_confirm)

    def below_outlier_is_ignored():
        """Ролик, не обошедший свою норму, в расчёт не идёт вовсе."""
        items = [reel(u, 1.1, f"Сколько стоит {u}") for u in ("a", "b", "c", "d")]
        assert producer.confirm(items) == [], "обычные ролики попали в тренды"
    expect("не выброс — не материал", below_outlier_is_ignored)

    def patterns_are_recognised():
        cases = [
            ("Что будет если не мыть машину год", "experiment"),
            ("До и после химчистки салона", "transformation"),
            ("Сколько стоит оклейка", "price"),
            ("Не делайте так со своей машиной", "mistake"),
            ("Что лучше: керамика или плёнка?", "question"),
            ("Вся оклейка за 60 секунд", "timelapse"),
            ("Oddly satisfying polishing", "satisfying"),
            ("Разбираем чужую работу", "reaction"),
        ]
        for text, want in cases:
            got = producer.detect(text)
            assert want in got, f"«{text}» → {got}, ожидался {want}"
    expect("приёмы распознаются по тексту", patterns_are_recognised)

    def tail_hashtags_do_not_trigger():
        """Приём виден в начале, а не в хвосте хештегов: иначе любой ролик
        с «#доипосле» в конце подписи объявлялся бы трансформацией."""
        long_tail = "Обычный рабочий день. " + "работаем " * 60 + "#доипосле #сколькостоит"
        assert producer.detect(long_tail) == [], producer.detect(long_tail)
    expect("хештеги в хвосте приёмом не считаются", tail_hashtags_do_not_trigger)

    def detailing_covers_every_pattern():
        """Пачка детейлинга ехала с продуктом с самого начала и обещает сценарий
        под каждый приём. Пропал сценарий — страница обещает и не даёт."""
        pack = niches.load("detailing")
        assert pack, "пачка детейлинга не читается"
        for p in producer.PATTERNS:
            assert producer.scripts_for(p["key"], pack), f"нет сценария для «{p['name']}»"
    expect("у детейлинга есть сценарий под каждый приём", detailing_covers_every_pattern)

    def scripts_are_timed_and_specific():
        """Проверяем все ниши в поставке: пачку дописывают руками в JSON,
        и опечатка там иначе всплывёт у клиента на экране."""
        for pack in niches.available():
            for key, scripts in pack["scripts"].items():
                assert producer.pattern_by_key(key), \
                    f"«{pack['key']}»: сценарии под несуществующий приём «{key}»"
                for s in scripts:
                    assert s["title"] and s["needs"], s
                    assert len(s["beats"]) >= 4, f"«{s['title']}»: сцен мало"
                    for time, beat in s["beats"]:
                        assert ":" in time, f"«{s['title']}»: нет тайминга у «{beat[:30]}»"
                        assert len(beat) > 25, f"«{s['title']}»: пустая подсказка «{beat}»"
    expect("сценарии с таймингом и по делу", scripts_are_timed_and_specific)

    def patterns_hold_without_a_niche():
        """Ниша не выбрана — приёмы всё равно остаются: они подтверждены
        цифрами конкурентов, а не тем, что мы знаем про бизнес клиента."""
        items = [reel(u, 3.0, f"Сколько стоит {u}") for u in ("a", "b", "c")]
        items.append(reel("a", 2.5, "Сколько стоит ещё"))
        g = producer.produce(items)[0]
        assert g["confirmed"], f"не подтвердилось: {g['missing']}"
        assert g["scripts"] == [], "без ниши взялись сценарии из ниоткуда"
        with_niche = producer.produce(items, niches.load("detailing"))[0]
        assert with_niche["scripts"], "с нишей сценарии не подставились"
    expect("без ниши приёмы остаются, сценариев нет", patterns_hold_without_a_niche)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
