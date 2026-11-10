"""Тренды ниши и встраивание роликов."""
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

    def embed_only_from_instagram():
        """Ссылка для iframe не должна вести куда попало."""
        good = q.embed_url("https://www.instagram.com/reel/Dc3sto1TFHy/")
        assert good == "https://www.instagram.com/reel/Dc3sto1TFHy/embed/", good
        for bad in ("https://evil.example.com/reel/AAA/", "javascript:alert(1)",
                    "мусор", "", None, "//instagram.com.evil.net/reel/AA/"):
            assert q.embed_url(bad) is None, f"пропущено: {bad!r}"
    expect("встраивается только настоящий адрес Instagram", embed_only_from_instagram)

    def embed_keeps_post_kind():
        """У фотопоста адрес /p/, и подменять его на /reel/ нельзя."""
        assert "/p/ABC123/embed/" in q.embed_url("https://www.instagram.com/p/ABC123/")
        assert "/reel/XyZ_9-a/embed/" in q.embed_url("https://www.instagram.com/reels/XyZ_9-a/")
    expect("тип записи в ссылке сохраняется", embed_keeps_post_kind)

    def embed_works_for_youtube_too():
        """Ролик YouTube тоже открывается на странице, а не «встроить нечего».
        Адрес собираем сами из кода ролика: ссылка приходит из ответа API,
        и подставлять её в iframe как есть нельзя."""
        for link in ("https://www.youtube.com/watch?v=SeiVz_QQ0Fo",
                     "https://youtu.be/SeiVz_QQ0Fo",
                     "https://www.youtube.com/shorts/SeiVz_QQ0Fo"):
            assert q.embed_url(link) == "https://www.youtube.com/embed/SeiVz_QQ0Fo", link
        for bad in ("https://www.youtube.com.evil.ru/watch?v=AAAAAAA",
                    "https://evil.example.com/watch?v=AAAAAAA"):
            assert q.embed_url(bad) is None, f"пропущено: {bad!r}"
    expect("ролик YouTube встраивается по коду, а не по чужой ссылке",
           embed_works_for_youtube_too)

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = db.connect(str(tmp))
    db.init(conn)
    conn.execute("INSERT INTO accounts (id, username, kind, followers_count)"
                 " VALUES (1,'own','own',500)")
    conn.execute("INSERT INTO accounts (id, username, kind, followers_count)"
                 " VALUES (2,'small','competitor',1000)")
    conn.execute("INSERT INTO accounts (id, username, kind, followers_count)"
                 " VALUES (3,'big','competitor',100000)")

    def add(mid, acc, views, when="2026-09-01T10:00:00+0000"):
        conn.execute("INSERT INTO media (id, account_id, ig_media_id, media_type,"
                     " product_type, posted_at, permalink) VALUES (?,?,?,'VIDEO','REELS',?,?)",
                     (mid, acc, f"m{mid}", when, f"https://www.instagram.com/reel/c{mid}/"))
        conn.execute("INSERT INTO media_stats (media_id, views, likes, comments, captured_at)"
                     " VALUES (?,?,?,?, '2026-09-10T00:00:00.000000+0000')",
                     (mid, views, views // 20, views // 500))

    # у мелкого автора медиана 1000, один ролик на 60 000 → множитель 60
    for i, v in enumerate([1000, 1000, 1000, 60000], start=10):
        add(i, 2, v)
    # у крупного медиана 100 000, ролик на 300 000 → множитель 3, но просмотров больше
    for i, v in enumerate([100000, 100000, 300000], start=20):
        add(i, 3, v)
    # свой аккаунт в тренды ниши попадать не должен
    add(30, 1, 999999)
    conn.commit()

    period = q.resolve_period("quarter")

    def own_account_excluded():
        ids = {i["id"] for i in q.trends(conn, period, limit=50)}
        assert 30 not in ids, "свой ролик попал в тренды конкурентов"
    expect("свой аккаунт в ленту ниши не попадает", own_account_excluded)

    def views_sort_favours_reach():
        top = q.trends(conn, period, sort="views", limit=50)[0]
        assert top["views"] == 300000, f"первым не самый просматриваемый: {top['views']}"
    expect("сортировка просмотрами даёт самый широкий ролик", views_sort_favours_reach)

    def multiplier_sort_favours_outlier():
        top = q.trends(conn, period, sort="multiplier", limit=50)[0]
        assert top["username"] == "small", f"первым не выброс: @{top['username']}"
        assert round(top["multiplier"]) == 60, f"множитель {top['multiplier']}"
    expect("сортировка множителем поднимает выброс мелкого автора", multiplier_sort_favours_outlier)

    def median_spans_whole_history():
        """Медиана берётся по всей истории автора, а не по выбранному отрезку.

        На коротком периоде у автора может быть два ролика, и медиана из двух
        значений — случайность, а не норма.
        """
        narrow = q.resolve_period("custom", "2026-09-01", "2026-09-01")
        top = [i for i in q.trends(conn, narrow, sort="multiplier", limit=50)
               if i["username"] == "small"][0]
        assert round(top["multiplier"]) == 60, f"медиана съехала: {top['multiplier']}"
    expect("медиана автора не зависит от выбранного периода", median_spans_whole_history)

    def noise_is_filtered():
        add(40, 2, 50)          # ниже порога
        conn.commit()
        assert 40 not in {i["id"] for i in q.trends(conn, period, limit=50)}
    expect("ролики ниже порога просмотров отсеиваются", noise_is_filtered)

    def erv_uses_views():
        top = [i for i in q.trends(conn, period, limit=50) if i["id"] == 22][0]
        # 300000 просмотров, 15000 лайков, 600 комментариев
        assert abs(top["er"] - (15600 / 300000)) < 1e-6, top["er"]
    expect("ERV считается к просмотрам", erv_uses_views)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
