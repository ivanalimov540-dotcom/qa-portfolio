"""Сторис и демография: код готов, ждём разблокировки приложения.

Заглушки повторяют боевую форму ответов Instagram, а не удобную нам: на
follows_and_unfollows мы уже обожглись — тест был написан под домысел, зеленел,
а в базе месяц лежали пустые подписки.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.ig import sync


class FakeClient:
    """Отвечает ровно так, как отвечает Graph API."""

    def __init__(self, fail_insights=False):
        self.calls_used = 0
        self.fail_insights = fail_insights

    def stories(self):
        self.calls_used += 1
        return {"data": [
            {"id": "111", "media_type": "VIDEO", "permalink": "https://instagram.com/stories/1",
             "thumbnail_url": "https://cdn/1.jpg", "timestamp": "2026-09-14T10:00:00+0000"},
            {"id": "222", "media_type": "IMAGE", "permalink": "https://instagram.com/stories/2",
             "media_url": "https://cdn/2.jpg", "timestamp": "2026-09-14T12:00:00+0000"},
        ]}

    def story_insights(self, story_id):
        """Обычные метрики. Боевой ответ приходит в старой форме values[],
        а не в total_value — проверено 16 сентября 2026."""
        self.calls_used += 1
        if self.fail_insights:
            from app.ig.client import GraphError
            raise GraphError("metrics not available yet")
        return {"data": [
            {"name": "reach", "period": "lifetime", "values": [{"value": 420}]},
            {"name": "views", "period": "lifetime", "values": [{"value": 501}]},
            {"name": "replies", "period": "lifetime", "values": [{"value": 7}]},
            {"name": "profile_visits", "period": "lifetime", "values": [{"value": 12}]},
        ]}

    def story_navigation(self, story_id):
        """Навигация отдельным запросом, ключи строчные — как в боевом ответе."""
        self.calls_used += 1
        if self.fail_insights:
            from app.ig.client import GraphError
            raise GraphError("metrics not available yet")
        return {"data": [{"name": "navigation", "values": [{"value": 369}],
                          "total_value": {"value": 369, "breakdowns": [
            {"dimension_keys": ["story_navigation_action_type"], "results": [
                {"dimension_values": ["tap_forward"], "value": 300},
                {"dimension_values": ["tap_back"], "value": 25},
                {"dimension_values": ["tap_exit"], "value": 4},
                {"dimension_values": ["swipe_forward"], "value": 40},
            ]}]}}]}

    def demographics(self, metric, breakdown="city", timeframe="this_month"):
        self.calls_used += 1
        if breakdown == "age":
            results = [{"dimension_values": ["25-34"], "value": 210},
                       {"dimension_values": ["35-44"], "value": 130}]
        elif breakdown == "gender":
            results = [{"dimension_values": ["M"], "value": 300},
                       {"dimension_values": ["F"], "value": 120}]
        else:
            results = [{"dimension_values": ["Rostov-on-Don, Rostov Oblast"], "value": 180}]
        return {"data": [{"name": metric, "total_value": {"breakdowns": [
            {"dimension_keys": [breakdown], "results": results}]}}]}


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
    conn.commit()

    def plain_metrics_come_in_old_shape():
        """Боевой ответ по обычным метрикам — values[], а не total_value."""
        vals = sync.story_values(FakeClient().story_insights("111"))
        assert vals["reach"] == 420 and vals["views"] == 501, vals
        assert vals["replies"] == 7 and vals["profile_visits"] == 12, vals
    expect("обычные метрики читаются в старой форме ответа", plain_metrics_come_in_old_shape)

    def navigation_unpacks():
        """Разбивка приходит строчными ключами и раскладывается обратно."""
        vals = sync.story_values(FakeClient().story_navigation("111"))
        assert vals["taps_forward"] == 300, vals
        assert vals["taps_back"] == 25, vals
        assert vals["exits"] == 4, vals
        assert vals["navigation"] == 369, vals
    expect("разбивка navigation раскладывается обратно", navigation_unpacks)

    def swipe_is_not_an_exit():
        """tap_exit — закрыл сторис совсем; swipe_forward — ушёл к следующему
        аккаунту. Сваливать их в одно значит путать «расхотелось смотреть»
        с «досмотрел и пошёл дальше»."""
        vals = sync.story_values(FakeClient().story_navigation("111"))
        assert vals["exits"] == 4, f"в выходы попал свайп: {vals['exits']}"
        assert vals["swipe_forward"] == 40, vals
    expect("свайп к следующему аккаунту не считается выходом", swipe_is_not_an_exit)

    def stories_are_written():
        rep = sync.sync_stories(conn, FakeClient(), 1)
        assert rep["found"] == 2 and rep["measured"] == 2, rep
        row = conn.execute("SELECT * FROM stories WHERE ig_story_id='111'").fetchone()
        assert row["reach"] == 420 and row["taps_forward"] == 300, dict(row)
        assert row["impressions"] == 501, "views не записались в impressions"
        assert row["swipe_forward"] == 40, "свайпы не записались"
        assert row["exits"] == 4, "в выходы попало лишнее"
    expect("сторис пишутся с метриками", stories_are_written)

    def story_without_metrics_is_still_saved():
        """Метрики появляются не сразу. Потерять сторис нельзя: через сутки
        её не вернуть ничем."""
        rep = sync.sync_stories(conn, FakeClient(fail_insights=True), 1)
        assert rep["found"] == 2 and rep["failed"] == 2, rep
        assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 2
    expect("сторис без метрик всё равно сохраняется", story_without_metrics_is_still_saved)

    def later_run_does_not_erase_metrics():
        """Прогон без метрик не должен затирать уже собранные."""
        row = conn.execute("SELECT reach FROM stories WHERE ig_story_id='111'").fetchone()
        assert row["reach"] == 420, f"метрики затёрлись пустым прогоном: {row['reach']}"
    expect("пустой замер не затирает собранные метрики", later_run_does_not_erase_metrics)

    def demographics_written():
        rep = sync.sync_demographics(conn, FakeClient(), 1)
        assert rep["written"] > 0, rep
        age = conn.execute(
            "SELECT value FROM demographics WHERE breakdown='age' AND value_key='25-34'"
            " AND metric='follower_demographics'").fetchone()
        assert age and age["value"] == 210, age
        city = conn.execute(
            "SELECT value_key FROM demographics WHERE breakdown='city' LIMIT 1").fetchone()
        assert "Rostov" in city["value_key"], city["value_key"]
    expect("демография пишется по всем разрезам", demographics_written)

    def geography_is_readable():
        """«SY» в списке стран и «Rostov-on-Don, Rostov Obla…» в колонке города —
        это не ответ на вопрос «кто на нас подписан». Коды переводим,
        а город держим отдельно от области, чтобы обрезалась область."""
        from app.web import queries as q
        day = conn.execute("SELECT MAX(date) FROM demographics").fetchone()[0]
        for code, n in (("RU", 300), ("SY", 40), ("ZZ", 5)):
            conn.execute(
                "INSERT OR REPLACE INTO demographics (account_id, date, metric,"
                " breakdown, value_key, value)"
                " VALUES (1, ?, 'follower_demographics', 'country', ?, ?)", (day, code, n))
        conn.commit()
        d = q.demographics(conn, 1)
        names = {c["value_key"]: c["label"] for c in d["countries"]}
        assert names.get("RU") == "Россия", names
        assert names.get("SY") == "Сирия", names
        assert names.get("ZZ") == "ZZ", \
            "незнакомый код надо оставлять как есть, а не выдумывать страну"
        city = d["cities"][0]
        assert city["city"] == "Rostov-on-Don" and city["region"] == "Rostov Oblast", city
    expect("страны названы по-русски, город отделён от области", geography_is_readable)

    def demographics_snapshot_is_idempotent():
        """Два прогона в один день — одна строка на срез, а не две."""
        before = conn.execute("SELECT COUNT(*) FROM demographics").fetchone()[0]
        sync.sync_demographics(conn, FakeClient(), 1)
        after = conn.execute("SELECT COUNT(*) FROM demographics").fetchone()[0]
        assert before == after, f"строки задвоились: {before} → {after}"
    expect("повторный прогон за день не задваивает срез", demographics_snapshot_is_idempotent)

    def demographics_absence_is_not_an_error():
        """До 100 подписчиков Instagram демографию не отдаёт — это норма."""
        class NoData(FakeClient):
            def demographics(self, metric, breakdown="city", timeframe="this_month"):
                from app.ig.client import GraphError
                raise GraphError("Insights are not available for this account")
        rep = sync.sync_demographics(conn, NoData(), 1)
        assert rep["written"] == 0 and rep["skipped"], rep
    expect("отсутствие демографии не считается сбоем", demographics_absence_is_not_an_error)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
