"""Сбор с YouTube: формы ответа, единицы и то, чего в публичном API нет."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.yt import sync as yt


class FakeClient:
    """Отвечает в форме YouTube Data API v3."""

    def __init__(self):
        self.calls_used = 0

    def channel(self, handle):
        return {
            "id": "UC" + "x" * 22,
            "snippet": {"title": "Тестовый канал", "description": "про машины",
                        "customUrl": "@testchannel",
                        "thumbnails": {"high": {"url": "https://i.ytimg.com/a.jpg"}}},
            "statistics": {"subscriberCount": "12300", "videoCount": "84"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UUxxx"}},
        }

    def uploads_playlist(self, channel):
        return channel["contentDetails"]["relatedPlaylists"]["uploads"]

    def playlist_video_ids(self, playlist_id, limit=50):
        return ["vid_short", "vid_long", "vid_hidden"]

    def videos(self, ids):
        return [
            {"id": "vid_short",
             "snippet": {"title": "Короткий", "description": "текст",
                         "publishedAt": "2026-09-01T10:00:00Z",
                         "thumbnails": {"high": {"url": "https://i.ytimg.com/s.jpg"}}},
             "contentDetails": {"duration": "PT45S"},
             "statistics": {"viewCount": "50000", "likeCount": "2000", "commentCount": "130"}},
            {"id": "vid_long",
             "snippet": {"title": "Длинный", "description": "",
                         "publishedAt": "2026-09-02T10:00:00Z", "thumbnails": {}},
             "contentDetails": {"duration": "PT12M30S"},
             "statistics": {"viewCount": "8000", "likeCount": "300", "commentCount": "20"}},
            # автор скрыл лайки и комментарии — ключей просто нет
            {"id": "vid_hidden",
             "snippet": {"title": "Без реакций", "description": "",
                         "publishedAt": "2026-09-03T10:00:00Z", "thumbnails": {}},
             "contentDetails": {"duration": "PT30S"},
             "statistics": {"viewCount": "1200"}},
        ]


def main() -> int:
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    def iso_duration():
        assert yt.duration_seconds("PT1M30S") == 90
        assert yt.duration_seconds("PT2H5M") == 7500
        assert yt.duration_seconds("") is None, "пустая строка должна давать None"
        assert yt.duration_seconds("30") is None, "секунды без формата — не длительность"
    expect("длительность ISO-8601 разбирается", iso_duration)

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = db.connect(str(tmp))
    db.init(conn)
    report = yt.sync_channel(conn, FakeClient(), "@testchannel")

    def channel_written():
        assert report["available"] and report["videos"] == 3, report
        row = conn.execute("SELECT * FROM accounts WHERE platform='youtube'").fetchone()
        assert row["username"] == "testchannel", row["username"]
        assert row["followers_count"] == 12300, row["followers_count"]
    expect("канал записан", channel_written)

    def platform_is_marked():
        """Без пометки сети ролики YouTube смешались бы с инстаграмными
        во всех выборках сразу."""
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT platform FROM media")}
        assert kinds == {"youtube"}, kinds
    expect("ролики помечены сетью", platform_is_marked)

    def shorts_detected_by_duration():
        """Флага Shorts у API нет — судим по длительности, как и сам YouTube."""
        short = conn.execute("SELECT product_type, duration_sec FROM media"
                             " WHERE ig_media_id='vid_short'").fetchone()
        long = conn.execute("SELECT product_type FROM media"
                            " WHERE ig_media_id='vid_long'").fetchone()
        assert short["product_type"] == "SHORTS", short["product_type"]
        assert short["duration_sec"] == 45, short["duration_sec"]
        assert long["product_type"] == "VIDEO", long["product_type"]
    expect("Shorts отличаются от длинных роликов", shorts_detected_by_duration)

    def hidden_reactions_stay_empty():
        """Скрытые лайки — это не ноль лайков. Подставить ноль значило бы
        объявить ролик безреакционным, не имея на то данных."""
        row = conn.execute(
            "SELECT s.likes, s.comments, s.views, s.total_interactions FROM media_stats s"
            " JOIN media m ON m.id = s.media_id WHERE m.ig_media_id='vid_hidden'").fetchone()
        assert row["views"] == 1200, row["views"]
        assert row["likes"] is None, f"скрытые лайки записаны как {row['likes']}"
        assert row["total_interactions"] is None, "взаимодействия выдуманы из воздуха"
    expect("скрытые реакции не подменяются нулём", hidden_reactions_stay_empty)

    def unavailable_metrics_are_absent():
        """Охвата, сохранений и удержания публичный API не отдаёт вовсе."""
        row = conn.execute(
            "SELECT s.reach, s.saved, s.skip_rate, s.avg_watch_time FROM media_stats s"
            " JOIN media m ON m.id = s.media_id WHERE m.ig_media_id='vid_short'").fetchone()
        assert row["reach"] is None and row["saved"] is None, dict(row)
        assert row["skip_rate"] is None and row["avg_watch_time"] is None, dict(row)
    expect("недоступные метрики остаются пустыми", unavailable_metrics_are_absent)

    def hidden_subscribers_are_not_zero():
        class Hidden(FakeClient):
            def channel(self, handle):
                c = super().channel(handle)
                c["statistics"] = {"hiddenSubscriberCount": True, "videoCount": "10"}
                c["id"] = "UC" + "y" * 22
                c["snippet"]["customUrl"] = "@hidden"
                return c
        yt.sync_channel(conn, Hidden(), "@hidden")
        row = conn.execute("SELECT followers_count FROM accounts"
                           " WHERE username='hidden'").fetchone()
        assert row["followers_count"] is None, f"скрытые подписчики = {row['followers_count']}"
    expect("скрытое число подписчиков не считается нулём", hidden_subscribers_are_not_zero)

    def repeat_run_does_not_duplicate():
        before = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        yt.sync_channel(conn, FakeClient(), "@testchannel")
        after = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        assert before == after, f"ролики задвоились: {before} → {after}"
    expect("повторный прогон не задваивает ролики", repeat_run_does_not_duplicate)

    def networks_never_mix():
        """Просмотр на YouTube и просмотр в Instagram — разные события.
        Таблица, где ростовский салон на 2 500 подписчиков стоит рядом
        с каналом на 495 000, не сравнивает ничего."""
        from app.web import queries as q
        conn.execute("INSERT INTO accounts (platform, username, kind, followers_count)"
                     " VALUES ('instagram','ig_shop','competitor',2500)")
        conn.execute("INSERT INTO media (platform, account_id, ig_media_id, media_type,"
                     " product_type, posted_at) VALUES ('instagram',"
                     " (SELECT id FROM accounts WHERE username='ig_shop'),"
                     " 'ig1','VIDEO','REELS','2026-09-01T10:00:00+0000')")
        conn.execute("INSERT INTO media_stats (media_id, views, likes, captured_at)"
                     " SELECT id, 5000, 100, '2026-09-10T00:00:00.000000+0000'"
                     " FROM media WHERE ig_media_id='ig1'")
        conn.commit()
        period = q.resolve_period("all")

        yt_rows = q.competitors(conn, period=period, platform="youtube")
        ig_rows = q.competitors(conn, period=period, platform="instagram")
        assert all(r["platform"] == "youtube" for r in yt_rows), "в YouTube попал чужой"
        assert all(r["platform"] == "instagram" for r in ig_rows), "в Instagram попал чужой"

        yt_tr = q.trends(conn, period, platform="youtube", limit=50)
        assert yt_tr, "тренды YouTube пусты"
        names = {t["username"] for t in yt_tr}
        assert "ig_shop" not in names, "инстаграмный аккаунт попал в тренды YouTube"
    expect("сети не смешиваются в сравнениях", networks_never_mix)

    def same_handle_on_two_networks():
        """Один бренд ведёт и Instagram, и YouTube под одним ником.
        При глобальной уникальности ника второй просто не добавлялся."""
        conn.execute("INSERT INTO accounts (platform, username, kind)"
                     " VALUES ('instagram','testchannel','competitor')")
        conn.commit()
        both = conn.execute(
            "SELECT platform FROM accounts WHERE username='testchannel'").fetchall()
        assert len(both) == 2, f"ник живёт только в одной сети: {len(both)}"
    expect("одинаковый ник в двух сетях не конфликтует", same_handle_on_two_networks)

    def instagram_sync_does_not_hijack_a_channel():
        """Ровно то, что случилось на живых данных: инстаграмный сбор искал
        аккаунт по одному нику, находил канал YouTube и писал под него свои
        посты. У семи каналов средние просмотры считались по смеси сетей."""
        from app.ig import sync as ig
        conn.execute("INSERT INTO accounts (platform, username, kind, ig_user_id)"
                     " VALUES ('youtube','demo_competitor','competitor','UC" + "z" * 22 + "')")
        conn.commit()
        channel = conn.execute(
            "SELECT id, ig_user_id FROM accounts WHERE platform='youtube'"
            " AND username='demo_competitor'").fetchone()

        account_id = ig.upsert_account(conn, "competitor",
                                       {"id": "17841400000000000", "username": "demo_competitor",
                                        "followers_count": 900, "media_count": 12})
        assert account_id != channel["id"], "инстаграм записался в канал YouTube"
        row = conn.execute("SELECT platform FROM accounts WHERE id=?", (account_id,)).fetchone()
        assert row["platform"] == "instagram", row["platform"]
        after = conn.execute("SELECT ig_user_id FROM accounts WHERE id=?",
                             (channel["id"],)).fetchone()
        assert after["ig_user_id"] == channel["ig_user_id"], \
            "у канала подменился идентификатор"

        media_id = ig.upsert_media(conn, account_id, {"id": "ig_1", "media_type": "VIDEO",
                                                      "timestamp": "2026-09-01T10:00:00+0000"})
        got = conn.execute("SELECT platform FROM media WHERE id=?", (media_id,)).fetchone()
        assert got["platform"] == "instagram", got["platform"]
    expect("инстаграмный сбор не занимает канал YouTube", instagram_sync_does_not_hijack_a_channel)

    def channel_id_heals_itself():
        """Неверный id в строке канала должен уходить при следующем сборе,
        а не жить до тех пор, пока его не найдут руками."""
        conn.execute("UPDATE accounts SET ig_user_id='17841400000000001'"
                     " WHERE platform='youtube' AND username='testchannel'")
        conn.commit()
        yt.sync_channel(conn, FakeClient(), "@testchannel")
        row = conn.execute("SELECT ig_user_id FROM accounts WHERE platform='youtube'"
                           " AND username='testchannel'").fetchone()
        assert row["ig_user_id"] == "UC" + "x" * 22, row["ig_user_id"]
    expect("чужой идентификатор канала исправляется сбором", channel_id_heals_itself)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
