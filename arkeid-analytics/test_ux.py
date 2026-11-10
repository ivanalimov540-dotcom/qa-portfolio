"""Подсказки на главном экране и оценка «много или мало»."""
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
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

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = db.connect(str(tmp))
    db.init(conn)
    conn.execute("INSERT INTO accounts (id, username, kind, followers_count)"
                 " VALUES (1,'own','own',500)")
    today = datetime.now(timezone.utc).date()

    def plural_forms():
        cases = [(1, "ролика"), (2, "роликов"), (5, "роликов"),
                 (21, "ролика"), (31, "ролика"), (11, "роликов"), (114, "роликов")]
        for n, want in cases:
            got = q.plural_ru(n, "ролика", "роликов", "роликов")
            assert got == want, f"{n} → {got}, ожидалось {want}"
    expect("числительные согласуются", plural_forms)

    def no_verdict_without_history():
        """На двух неделях медиана дня ещё случайна — оценку не даём."""
        for i in range(5):
            conn.execute("INSERT INTO account_stats (account_id, date, reach, views)"
                         " VALUES (1,?,100,200)",
                         ((today - timedelta(days=i)).isoformat(),))
        conn.commit()
        assert q.versus_normal(conn, 1, q.resolve_period("week")) == {}, \
            "оценка выдана на пяти днях истории"
    expect("на короткой истории оценка не выдаётся", no_verdict_without_history)

    def compares_average_day_not_sum():
        """Сравнивать сумму с суммой нельзя: у периодов разная длина,
        и «больше» означало бы только «период длиннее»."""
        conn.execute("DELETE FROM account_stats")
        # 60 обычных дней по 100 охвата
        for i in range(7, 67):
            conn.execute("INSERT INTO account_stats (account_id, date, reach, views)"
                         " VALUES (1,?,100,200)",
                         ((today - timedelta(days=i)).isoformat(),))
        # последняя неделя — по 300, то есть втрое выше обычного
        for i in range(7):
            conn.execute("INSERT INTO account_stats (account_id, date, reach, views)"
                         " VALUES (1,?,300,600)",
                         ((today - timedelta(days=i)).isoformat(),))
        conn.commit()
        # Ровно семь высоких дней: пресет «неделя» покрывает восемь календарных
        # (так было и до появления диапазонов), и в него попал бы обычный день.
        period = q.resolve_period("custom", (today - timedelta(days=6)).isoformat(),
                                  today.isoformat())
        week = q.versus_normal(conn, 1, period)
        assert week["reach"]["level"] == "good", week["reach"]
        assert abs(week["reach"]["per_day"] - 300) < 1, week["reach"]["per_day"]
        assert abs(week["reach"]["normal"] - 100) < 1, week["reach"]["normal"]
    expect("сравнивается средний день, а не сумма", compares_average_day_not_sum)

    def band_around_normal_is_not_a_change():
        """Отклонение в несколько процентов — шум, а не рост."""
        conn.execute("DELETE FROM account_stats")
        for i in range(60):
            reach = 105 if i < 7 else 100      # +5%
            conn.execute("INSERT INTO account_stats (account_id, date, reach)"
                         " VALUES (1,?,?)",
                         ((today - timedelta(days=i)).isoformat(), reach))
        conn.commit()
        week = q.versus_normal(conn, 1, q.resolve_period("week"))
        assert week["reach"]["level"] == "mid", week["reach"]
        assert week["reach"]["word"] == "как обычно", week["reach"]["word"]
    expect("небольшое отклонение не выдаётся за рост", band_around_normal_is_not_a_change)

    def urgent_steps_come_first():
        """Список одинаковых ссылок перестают замечать; наверх встаёт то,
        что именно сейчас выглядит хуже всего."""
        for i in range(1, 30):
            conn.execute("INSERT INTO media (id, account_id, ig_media_id, media_type,"
                         " product_type, posted_at) VALUES (?,1,?,'VIDEO','REELS',?)",
                         (i, f"m{i}", (today - timedelta(days=i)).isoformat()))
            conn.execute("INSERT INTO media_stats (media_id, skip_rate, captured_at)"
                         " VALUES (?, 0.8, '2026-09-10T00:00:00.000000+0000')", (i,))
        conn.commit()
        steps = q.next_steps(conn, 1, {})
        assert steps, "подсказок нет"
        assert steps[0]["urgent"], f"первым идёт неважное: {steps[0]['q']}"
        assert all(s.get("endpoint") for s in steps), "подсказка без ссылки"
    expect("срочное поднимается наверх списка", urgent_steps_come_first)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
