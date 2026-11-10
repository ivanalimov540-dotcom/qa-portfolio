"""Цели: выполнение, «идём ли по графику», защита от мусора в форме."""
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
    conn.execute("INSERT INTO accounts (id, username, kind) VALUES (1,'own','own')")

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=9)     # идёт десятый день
    end = today + timedelta(days=10)      # всего 20 дней, прошло ровно половина
    for i in range(10):
        conn.execute("INSERT INTO account_stats (account_id, date, reach) VALUES (1,?,100)",
                     ((start + timedelta(days=i)).isoformat(),))
    conn.commit()

    def half_done_at_half_time_is_on_track():
        """Половина цели на половине срока — это график, а не отставание."""
        q.kpi_add(conn, 1, "reach", 2000, start, end)
        g = q.kpi_list(conn, 1)[0]
        assert g["fact"] == 1000, g["fact"]
        assert 0.45 < g["elapsed"] < 0.55, g["elapsed"]
        assert g["status"] == "on_track", f"статус {g['status']}"
    expect("половина цели на половине срока — идём по графику", half_done_at_half_time_is_on_track)

    def far_behind_is_behind():
        q.kpi_add(conn, 1, "views", 100000, start, end)
        g = [x for x in q.kpi_list(conn, 1) if x["metric"] == "views"][0]
        assert g["status"] == "behind", f"статус {g['status']}"
    expect("сильное отставание видно", far_behind_is_behind)

    def finished_period_is_judged_by_result():
        past_start, past_end = today - timedelta(days=40), today - timedelta(days=30)
        q.kpi_add(conn, 1, "requests", 5, past_start, past_end)
        g = [x for x in q.kpi_list(conn, 1) if x["metric"] == "requests"][0]
        assert g["finished"], "срок должен считаться законченным"
        assert g["status"] == "missed", f"статус {g['status']}"
    expect("законченный срок оценивается по факту, а не по графику", finished_period_is_judged_by_result)

    def garbage_is_rejected():
        before = len(q.kpi_list(conn, 1))
        assert not q.kpi_add(conn, 1, "выдумка", 100, start, end), "принята несуществующая метрика"
        assert not q.kpi_add(conn, 1, "reach", -5, start, end), "принята отрицательная цель"
        assert not q.kpi_add(conn, 1, "reach", 100, end, start), "приняты даты наоборот"
        assert not q.kpi_add(conn, 1, "reach", 100, None, end), "принята пустая дата"
        assert len(q.kpi_list(conn, 1)) == before, "мусор всё-таки записался"
    expect("мусор в форме не записывается", garbage_is_rejected)

    def delete_removes_only_own():
        g = q.kpi_list(conn, 1)[0]
        q.kpi_delete(conn, 999, g["id"])          # чужой аккаунт
        assert any(x["id"] == g["id"] for x in q.kpi_list(conn, 1)), "удалили чужой цель"
        q.kpi_delete(conn, 1, g["id"])
        assert not any(x["id"] == g["id"] for x in q.kpi_list(conn, 1)), "своя цель не удалилась"
    expect("удалить можно только свою цель", delete_removes_only_own)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
