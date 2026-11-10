"""Сбор с Threads: формы ответа и то, чего у этой сети нет."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.threads import sync as th


class FakeClient:
    """Отвечает в форме Threads API."""

    def __init__(self, fail_insights=False, old_shape=False):
        self.calls_used = 0
        self.fail_insights = fail_insights
        self.old_shape = old_shape

    def whoami(self):
        self.calls_used += 1
        return {"id": "9876543210", "username": "demo_detailing",
                "name": "Demo Detailing",
                "threads_biography": "детейлинг в Ростове",
                "threads_profile_picture_url": "https://cdn/pic.jpg"}

    def user_insights(self, since="", until=""):
        self.calls_used += 1
        return {"data": [{"name": "followers_count", "total_value": {"value": 412}}]}

    def posts(self, limit=50, after=""):
        self.calls_used += 1
        return {"data": [
            {"id": "p1", "media_type": "TEXT_POST", "media_product_type": "THREADS",
             "text": "Оклейка за один день", "permalink": "https://threads.net/p1",
             "timestamp": "2026-09-10T10:00:00+0000"},
            {"id": "p2", "media_type": "IMAGE", "media_product_type": "THREADS",
             "text": "До и после", "permalink": "https://threads.net/p2",
             "thumbnail_url": "https://cdn/p2.jpg",
             "timestamp": "2026-09-11T10:00:00+0000"},
        ]}

    def post_insights(self, post_id):
        self.calls_used += 1
        if self.fail_insights:
            from app.threads.client import ThreadsError
            raise ThreadsError("insights not available yet")
        if self.old_shape:
            return {"data": [
                {"name": "views", "values": [{"value": 3100}]},
                {"name": "likes", "values": [{"value": 88}]},
                {"name": "replies", "values": [{"value": 12}]},
                {"name": "reposts", "values": [{"value": 5}]},
                {"name": "quotes", "values": [{"value": 2}]},
            ]}
        return {"data": [
            {"name": "views", "total_value": {"value": 3100}},
            {"name": "likes", "total_value": {"value": 88}},
            {"name": "replies", "total_value": {"value": 12}},
            {"name": "reposts", "total_value": {"value": 5}},
            {"name": "quotes", "total_value": {"value": 2}},
        ]}


def main() -> int:
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    def both_shapes_read():
        """Threads отдаёт метрики то в total_value, то в values[] — как
        Instagram на заре. Смена формы не должна ронять сбор молча."""
        new = th.insight_values(FakeClient().post_insights("p1"))
        old = th.insight_values(FakeClient(old_shape=True).post_insights("p1"))
        assert new == old, f"формы читаются по-разному: {new} против {old}"
        assert new["views"] == 3100 and new["quotes"] == 2, new
    expect("обе формы ответа читаются одинаково", both_shapes_read)

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = db.connect(str(tmp))
    db.init(conn)
    report = th.sync_own(conn, FakeClient())

    def profile_and_posts():
        assert report["posts"] == 2 and report["measured"] == 2, report
        acc = conn.execute("SELECT * FROM accounts WHERE platform='threads'").fetchone()
        assert acc["username"] == "demo_detailing", acc["username"]
        assert acc["kind"] == "own", "профиль Threads должен быть своим"
        assert acc["followers_count"] == 412, acc["followers_count"]
    expect("профиль и посты записаны", profile_and_posts)

    def quotes_counted_as_shares():
        """Цитата — репост со своим текстом, то есть передача дальше.
        Заводить колонку ради одной сети незачем, смысл совпадает."""
        row = conn.execute(
            "SELECT s.* FROM media_stats s JOIN media m ON m.id=s.media_id"
            " WHERE m.ig_media_id='p1'").fetchone()
        assert row["shares"] == 2, f"цитаты не легли в shares: {row['shares']}"
        assert row["reposts"] == 5, row["reposts"]
        assert row["comments"] == 12, "ответы должны лечь в комментарии"
        assert row["total_interactions"] == 88 + 12 + 5 + 2, row["total_interactions"]
    expect("цитаты и ответы разложены по смыслу", quotes_counted_as_shares)

    def absent_metrics_stay_empty():
        """У Threads нет охвата, сохранений и удержания — это текстовая лента."""
        row = conn.execute(
            "SELECT s.reach, s.saved, s.skip_rate FROM media_stats s"
            " JOIN media m ON m.id=s.media_id WHERE m.ig_media_id='p1'").fetchone()
        assert row["reach"] is None and row["saved"] is None, dict(row)
        assert row["skip_rate"] is None, dict(row)
    expect("отсутствующие метрики не подменяются нулём", absent_metrics_stay_empty)

    def post_survives_missing_insights():
        """Метрики свежего поста появляются с задержкой. Потерять сам пост
        из-за этого нельзя."""
        tmp2 = Path(tempfile.mkdtemp()) / "t2.db"
        c2 = db.connect(str(tmp2))
        db.init(c2)
        rep = th.sync_own(c2, FakeClient(fail_insights=True))
        assert rep["posts"] == 2 and rep["failed"] == 2, rep
        assert c2.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2
    expect("пост сохраняется даже без метрик", post_survives_missing_insights)

    def platform_is_marked():
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT platform FROM media")}
        assert kinds == {"threads"}, kinds
    expect("посты помечены сетью", platform_is_marked)

    def repeat_run_does_not_duplicate():
        before = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        th.sync_own(conn, FakeClient())
        after = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        assert before == after, f"посты задвоились: {before} → {after}"
    expect("повторный прогон не задваивает посты", repeat_run_does_not_duplicate)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
