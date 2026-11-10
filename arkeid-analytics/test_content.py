"""Закономерности контента: разбор ролика, время, хештеги, форматы."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import advisor, db
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

    med = {"hook": (0.4, 0.6), "retention": (0.3, 0.7),
           "reach": (500, 1500), "saved": (1, 4)}

    def weak_hook_stops_the_chain():
        """У ролика со слабым началом не обсуждаем сохранения: до них не дошли."""
        v = advisor.verdict({"hook": 0.2, "retention": 0.9, "reach": 200, "saved": 0,
                             "hook_text": "тест"}, med)
        stages = [s["stage"] for s in v["steps"]]
        assert "Сохранения" not in stages, f"ступени: {stages}"
        assert "Досмотр" not in stages, "досмотр обсуждать рано, если ушли на третьей секунде"
    expect("слабый хук обрывает разбор дальше по воронке", weak_hook_stops_the_chain)

    def good_reel_has_no_fixes():
        v = advisor.verdict({"hook": 0.9, "retention": 0.9, "reach": 3000, "saved": 10,
                             "hook_text": "тест"}, med)
        assert v["tone"] == "good", v["tone"]
        assert not v["fixes"], f"у удачного ролика нечего чинить: {v['fixes']}"
    expect("у сработавшего ролика нет списка правок", good_reel_has_no_fixes)

    def silent_reel_gets_visual_advice():
        """Ролику без речи бессмысленно советовать убрать приветствие."""
        v = advisor.verdict({"hook": 0.2, "retention": 0.5, "reach": 200, "saved": 0,
                             "hook_text": "", "hook_is_soundtrack": True}, med)
        assert v["silent"]
        assert any("кадр" in f for f in v["fixes"]), v["fixes"]
        assert not any("приветствие" in f for f in v["fixes"]), "совет про речь у немого ролика"
    expect("немому ролику советуем картинку, а не текст", silent_reel_gets_visual_advice)

    def missing_hook_is_not_a_failure():
        v = advisor.verdict({"hook": None, "retention": None, "reach": None, "saved": None}, med)
        assert v["steps"], "разбор пуст"
        assert "не отдал" in v["steps"][0]["text"], v["steps"][0]["text"]
    expect("отсутствие данных не выдаётся за плохой результат", missing_hook_is_not_a_failure)

    def hashtag_rejects_numbers():
        """Instagram не допускает тег без буквы, а в подписях полно номеров."""
        found = q.HASHTAG_RE.findall("звоните #637344830 про #детейлинг и #ppf2024")
        assert "637344830" not in found, found
        assert "детейлинг" in found and "ppf2024" in found, found
    expect("чисто цифровой тег не считается хештегом", hashtag_rejects_numbers)

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = db.connect(str(tmp))
    db.init(conn)
    conn.execute("INSERT INTO accounts (id, username, kind) VALUES (1,'own','own')")
    conn.execute("INSERT INTO accounts (id, username, kind) VALUES (2,'big','competitor')")
    conn.execute("INSERT INTO accounts (id, username, kind) VALUES (3,'small','competitor')")

    def post(mid, acc, fmt, views, likes, when):
        conn.execute("INSERT INTO media (id, account_id, ig_media_id, media_type,"
                     " product_type, posted_at) VALUES (?,?,?,'VIDEO',?,?)",
                     (mid, acc, f"m{mid}", fmt, when))
        conn.execute("INSERT INTO media_stats (media_id, views, likes, comments, reach,"
                     " captured_at) VALUES (?,?,?,0,?, '2026-09-10T00:00:00.000000+0000')",
                     (mid, views, likes, views))

    # крупный аккаунт снимает только Reels, мелкий — только ленту
    for i in range(10):
        post(100 + i, 2, "REELS", 100000, 5000, "2026-09-01T12:00:00+0000")
    for i in range(10):
        post(200 + i, 3, "FEED", None, 10, "2026-09-01T12:00:00+0000")
    conn.commit()

    def format_normalised_by_author():
        """Без нормировки вышло бы «Reels лучше ленты», а на деле это «Reels
        снимает крупный аккаунт»."""
        fa = q.format_analysis(conn, q.resolve_period("all"))
        feed = [f for f in fa["niche"] if f["fmt"] == "FEED"][0]
        assert feed["no_views"], "у ленты просмотров нет, это надо показывать явно"
        assert feed["median_mult"] is None, "нет данных подменили нулём"
        assert feed["median_likes"] == 10, feed["median_likes"]
    expect("нет просмотров — это не ноль просмотров", format_normalised_by_author)

    def best_time_marks_thin_slots():
        for i in range(4):
            post(300 + i, 1, "REELS", 1000, 10, f"2026-09-0{i+1}T15:00:00+0000")
        post(310, 1, "REELS", 9000, 10, "2026-09-06T03:00:00+0000")
        conn.commit()
        bt = q.best_time(conn, 1)
        thin = [h for h in bt["hours"] if not h["enough"]]
        assert any(h["key"] == 3 for h in thin), "слот из одного ролика не помечен"
        assert bt["best_hour"]["key"] == 15, f"лучшим часом стал случайный: {bt['best_hour']}"
    expect("слот из одного ролика не становится лучшим временем", best_time_marks_thin_slots)

    def niche_needs_no_benchmark_pick():
        """Сравнение с нишей должно даваться само: ориентир надо сначала
        назначить, и пока его нет, сравнения не существовало вовсе."""
        conn.execute("INSERT INTO accounts (id, username, kind, followers_count)"
                     " VALUES (7,'peer1','competitor',5000)")
        conn.execute("INSERT INTO accounts (id, username, kind, followers_count)"
                     " VALUES (8,'peer2','competitor',5000)")
        for acc, views in ((7, 50000), (8, 70000)):
            for i in range(6):
                post(400 + acc * 10 + i, acc, "REELS", views, views // 20,
                     "2026-09-01T12:00:00+0000")
        conn.commit()
        n = q.niche_position(conn, 1, q.resolve_period("all"))
        assert n["enough"], "сравнение не посчиталось"
        assert not any(r["key"] in ("reach", "saved") for r in n["rows"]), \
            "в сравнение попал показатель, которого по чужим аккаунтам нет"
        views_row = [r for r in n["rows"] if r["key"] == "avg_views"]
        assert views_row, "средние просмотры не сравнились"
        assert views_row[0]["ahead"] is False, "наш аккаунт не может быть выше при 1000 против 60000"
    expect("сравнение с нишей не требует выбора ориентира", niche_needs_no_benchmark_pick)

    def signed_in_client():
        """Сайт закрыт паролем, и выгрузка — тоже. Без входа тестовый клиент
        получал бы редирект на форму входа вместо файла."""
        from app.web.server import app
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 1
        return client

    def export_requires_login():
        from app.web.server import app
        app.config["TESTING"] = True
        r = app.test_client().get("/export/reels.csv")
        assert r.status_code in (302, 401, 403), \
            f"выгрузка отдаётся без входа: {r.status_code}"
    expect("выгрузка закрыта входом", export_requires_login)

    def csv_opens_in_excel():
        """Русский Excel: без BOM кириллица становится кракозябрами,
        без точки с запятой всё встаёт в один столбец."""
        body = signed_in_client().get("/export/reels.csv").get_data(as_text=True)
        assert body.startswith("\ufeff"), "нет BOM — Excel покажет кракозябры"
        head = body.splitlines()[0]
        assert ";" in head and "," not in head.split("ссылка")[0], f"разделитель не тот: {head}"
    expect("CSV открывается в русском Excel", csv_opens_in_excel)

    def export_respects_period():
        c2 = signed_in_client()
        wide = len(c2.get("/export/reels.csv?period=all").get_data(as_text=True).splitlines())
        narrow = len(c2.get("/export/reels.csv?period=week").get_data(as_text=True).splitlines())
        assert narrow <= wide, f"неделя ({narrow}) шире, чем всё время ({wide})"
        name = c2.get("/export/daily.csv?period=custom&from=2026-08-01&to=2026-08-31") \
                 .headers.get("Content-Disposition", "")
        assert "2026-08-01" in name and "2026-08-31" in name, name
    expect("выгрузка считается за выбранный период", export_respects_period)

    def unknown_export_is_404():
        assert signed_in_client().get("/export/anything.csv").status_code == 404
    expect("неизвестная выгрузка не выдумывается", unknown_export_is_404)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
