"""Защиты от ложных выводов: сравнение периодов и текст фонограммы."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
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

    def short_previous_is_not_comparable():
        # ровно тот случай, что показывал «охват вырос на 770%»:
        # 31 день текущего периода против одного дня прошлого
        assert not q.comparable(list(range(31)), [1])
    expect("месяц против одного дня не сравнивается", short_previous_is_not_comparable)

    def full_previous_is_comparable():
        assert q.comparable(list(range(30)), list(range(30)))
    expect("полный прошлый период сравнивается", full_previous_is_comparable)

    def slightly_short_still_ok():
        # мелкий пропуск в истории не должен убивать сравнение
        assert q.comparable(list(range(30)), list(range(27)))
    expect("пара пропущенных дней сравнению не мешает", slightly_short_still_ok)

    def empty_previous():
        assert not q.comparable([1, 2, 3], [])
        assert not q.comparable([], [1, 2, 3])
    expect("пустой период не сравнивается", empty_previous)

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = db.connect(str(tmp))
    db.init(conn)
    conn.execute("INSERT INTO accounts (id, username, kind) VALUES (1,'a','own')")
    for i in (1, 2, 3):
        conn.execute(
            "INSERT INTO media (id, account_id, ig_media_id, media_type, posted_at)"
            " VALUES (?,1,?,'REELS','2026-09-01T00:00:00+0000')", (i, f"m{i}"))
    # один и тот же текст на двух роликах — это трек, а не слова автора
    for i in (1, 2):
        conn.execute(
            "INSERT INTO transcripts (media_id, status, hook_text, text)"
            " VALUES (?, 'done', 'А я бегу на красный свет', 'А я бегу на красный свет')", (i,))
    conn.execute(
        "INSERT INTO transcripts (media_id, status, hook_text, text)"
        " VALUES (3, 'done', 'Проверьте толщину лака перед полировкой', 'текст')")
    conn.commit()

    def soundtrack_detected():
        dupes = q.duplicate_hooks(conn, 1)
        assert "А я бегу на красный свет" in dupes, "повтор не распознан как фонограмма"
        assert "Проверьте толщину лака перед полировкой" not in dupes, "уникальный хук помечен зря"
    expect("повторяющийся хук считается фонограммой", soundtrack_detected)

    def single_reel_page_drops_soundtrack():
        item = q.reel(conn, 1)
        assert item is not None, "ролик не найден"
        assert item.get("hook_is_soundtrack"), "страница ролика не пометила фонограмму"
        assert not item.get("hook_text"), "текст песни остался хуком — его будут советовать повторить"
    expect("страница ролика не выдаёт песню за хук", single_reel_page_drops_soundtrack)

    def real_hook_survives():
        item = q.reel(conn, 3)
        assert item["hook_text"] == "Проверьте толщину лака перед полировкой"
        assert not item.get("hook_is_soundtrack")
    expect("настоящий хук не трогается", real_hook_survives)

    def health_names_the_token_as_the_cause():
        conn.execute("INSERT INTO sync_jobs (id, kind, status, started_at, finished_at)"
                     " VALUES (1,'own','done','2026-09-01 00:00:00','2026-09-01T00:10:00+0000')")
        # сетевой сбой раньше, мёртвый токен позже — причиной должен считаться последний
        conn.execute("INSERT INTO sync_jobs (id, kind, status, started_at, error)"
                     " VALUES (2,'own','error','2026-09-02 00:00:00','Соединение не установлено')")
        conn.execute("INSERT INTO sync_jobs (id, kind, status, started_at, error)"
                     " VALUES (3,'own','error','2026-09-03 00:00:00',"
                     "'Error validating access token: The session has been invalidated')")
        conn.commit()
        h = q.data_health(conn)
        assert h["failures"] == 2, f"неудач посчитано {h['failures']}"
        assert h["token_dead"], "мёртвый токен не распознан как причина"
        assert h["alarm"], "тревога не поднята"
    expect("причиной названа последняя ошибка, а не алфавитно старшая", health_names_the_token_as_the_cause)

    def network_failure_is_not_a_dead_token():
        conn.execute("DELETE FROM sync_jobs WHERE id = 3")
        conn.commit()
        h = q.data_health(conn)
        assert not h["token_dead"], "сетевой сбой принят за мёртвый токен"
        assert "Соединение" in h["error"]
    expect("сетевой сбой не выдаётся за мёртвый токен", network_failure_is_not_a_dead_token)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
