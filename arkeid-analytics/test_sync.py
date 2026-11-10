"""Проверка модуля сбора на поддельных ответах Graph API.

Токен не нужен: подставляем ответы в том виде, в каком их описывает
документация, и смотрим, что оказалось в базе.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.ig import sync


def ts(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeClient:
    """Отдаёт ответы обеих форм: total_value и values — Meta использует и ту, и другую."""

    def __init__(self):
        self.calls_used = 0

    def whoami(self):
        self.calls_used += 1
        return {"id": "17841400000000000", "username": "demo_detailing",
                "name": "Demo Detailing", "followers_count": 3240, "media_count": 48}

    def account_insights(self, since="", until=""):
        self.calls_used += 1
        return {"data": [
            {"name": "reach", "total_value": {"value": 9100}},
            {"name": "views", "total_value": {"value": 14200}},
            {"name": "accounts_engaged", "values": [{"value": 640}]},
            {"name": "total_interactions", "total_value": {"value": 512}},
        ]}

    def account_insight_breakdown(self, metric, since="", until=""):
        self.calls_used += 1
        if metric == "profile_links_taps":
            return {"data": [{"name": "profile_links_taps", "total_value": {"breakdowns": [
                {"dimension_keys": ["contact_button_type"], "results": [
                    {"dimension_values": ["TEXT"], "value": 7},
                    {"dimension_values": ["CALL"], "value": 3},
                    {"dimension_values": ["DIRECTION"], "value": 5},
                    {"dimension_values": ["WEBSITE"], "value": 40},
                ]}]}}]}
        # Точная форма боевого ответа: ключи FOLLOWER/NON_FOLLOWER, а не
        # FOLLOW/UNFOLLOW. Раньше здесь стояли выдуманные ключи, тест зелёнел,
        # а в базе месяц лежали пустые подписки.
        return {"data": [{"name": "follows_and_unfollows", "total_value": {"breakdowns": [
            {"dimension_keys": ["follow_type"], "results": [
                {"dimension_values": ["FOLLOWER"], "value": 26},
                {"dimension_values": ["NON_FOLLOWER"], "value": 11},
            ]}]}}]}

    def own_media(self, limit=50, after=""):
        self.calls_used += 1
        return {"data": [
            {"id": "own_new", "media_type": "VIDEO", "media_product_type": "REELS",
             "caption": "свежий", "timestamp": ts(2), "media_url": "https://cdn/a.mp4"},
            {"id": "own_old", "media_type": "VIDEO", "media_product_type": "REELS",
             "caption": "старый", "timestamp": ts(200), "media_url": "https://cdn/b.mp4"},
        ]}

    def media_insights(self, media_id, product_type):
        self.calls_used += 1
        return {"data": [
            {"name": "views", "total_value": {"value": 5200}},
            {"name": "reach", "total_value": {"value": 4100}},
            {"name": "saved", "total_value": {"value": 88}},
            {"name": "reels_skip_rate", "total_value": {"value": 0.31}},
            {"name": "ig_reels_avg_watch_time", "values": [{"value": 12.4}]},
        ]}

    def business_discovery(self, username, media_limit=50, after=""):
        self.calls_used += 1
        if username == "closed_account":
            return {}
        media = []
        for i in range(6):
            views = 1000 if i else 9000          # первый — выброс, ×~7 к медиане
            media.append({"id": f"{username}_{i}", "media_type": "VIDEO",
                          "media_product_type": "REELS", "caption": f"пост {i}",
                          "timestamp": ts(i * 5), "like_count": views // 20,
                          "comments_count": 5, "view_count": views})
        return {"id": f"ig_{username}", "username": username, "name": username.title(),
                "followers_count": 8400, "media_count": 120, "media": {"data": media}}


def check(condition, message):
    assert condition, message


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    conn = db.connect(tmp)
    db.init(conn)
    client = FakeClient()
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    result = sync.sync_own(conn, client)

    def own_account():
        row = conn.execute("SELECT * FROM accounts WHERE kind='own'").fetchone()
        check(row["username"] == "demo_detailing", "не тот ник")
        check(row["followers_count"] == 3240, "подписчики не записались")
    expect("свой аккаунт записан", own_account)

    def taps():
        row = conn.execute("SELECT * FROM account_stats").fetchone()
        breakdown = json.loads(row["link_taps_json"])
        # TEXT/CALL/DIRECTION приведены к единым именам
        check(breakdown["text_message_clicks"] == 7, "директ не разобрался")
        check(breakdown["get_directions_clicks"] == 5, "маршрут не разобрался")
        # заявка = директ + звонок + маршрут, без website_clicks
        check(row["link_taps_total"] == 15, f"заявок {row['link_taps_total']}, ожидалось 15")
    expect("заявки посчитаны без website_clicks", taps)

    def follows():
        row = conn.execute("SELECT follows, unfollows FROM account_stats").fetchone()
        check(row["follows"] == 26 and row["unfollows"] == 11, "подписки/отписки не разобрались")
    expect("подписки и отписки разобраны", follows)

    def follows_legacy_keys():
        # Meta уже переименовывала эти ключи. Старое написание тоже читаем.
        got = (sync._first({"FOLLOW": 5}, sync.FOLLOW_KEYS),
               sync._first({"UNFOLLOW": 2}, sync.UNFOLLOW_KEYS))
        check(got == (5, 2), f"старые ключи не прочитались: {got}")
        check(sync._first({}, sync.FOLLOW_KEYS) is None, "пустая разбивка должна давать None")
    expect("старые названия ключей тоже понимаются", follows_legacy_keys)

    def both_shapes():
        row = conn.execute("SELECT accounts_engaged FROM account_stats").fetchone()
        check(row["accounts_engaged"] == 640, "форма values не разобралась")
    expect("обе формы ответа Insights читаются", both_shapes)

    def old_skipped():
        # старый пост сохранён, но метрики на него не запрашивались
        old = conn.execute("SELECT id FROM media WHERE ig_media_id='own_old'").fetchone()
        stats = conn.execute("SELECT count(*) c FROM media_stats WHERE media_id=?", (old["id"],)).fetchone()
        check(stats["c"] == 0, "на старый пост зря потрачен вызов")
        check(result["refreshed"] == 1, f"обновлено {result['refreshed']}, ожидался 1")
    expect("старые посты не тратят лимит", old_skipped)

    def snapshots_accumulate():
        before = conn.execute("SELECT count(*) c FROM media_stats").fetchone()["c"]
        media_id = conn.execute("SELECT id FROM media WHERE ig_media_id='own_new'").fetchone()["id"]
        sync.write_media_stats(conn, media_id, {"views": 6000})
        after = conn.execute("SELECT count(*) c FROM media_stats").fetchone()["c"]
        check(after == before + 1, "снимок не добавился — история теряется")
    expect("метрики накапливаются, а не перезаписываются", snapshots_accumulate)

    ok = sync.sync_competitor(conn, client, "detailing_garage161")
    def competitor():
        check(ok["available"] and ok["posts"] == 6, f"конкурент собран неверно: {ok}")
        row = conn.execute("SELECT * FROM accounts WHERE username='detailing_garage161'").fetchone()
        check(row["kind"] == "competitor", "не та роль")
    expect("конкурент собран", competitor)

    conn.execute("INSERT INTO accounts (username, kind) VALUES ('closed_account','competitor')")
    conn.commit()
    closed = sync.sync_competitor(conn, client, "closed_account")
    def unavailable():
        check(not closed["available"], "закрытый аккаунт помечен доступным")
        row = conn.execute("SELECT is_available, unavailable_reason FROM accounts WHERE username='closed_account'").fetchone()
        check(row["is_available"] == 0 and row["unavailable_reason"], "причина не записана")
    expect("закрытый аккаунт помечен, а не уронил прогон", unavailable)

    def outliers_only():
        acc = conn.execute("SELECT id FROM accounts WHERE username='detailing_garage161'").fetchone()
        picked = sync.competitor_reels_to_transcribe(conn, acc["id"])
        check(len(picked) == 1, f"выбрано {len(picked)} роликов, ожидался 1 выброс")
        check(picked[0]["multiplier"] >= 2, "выбран не выброс")
        check(picked[0]["views"] == 9000, "выбран не тот ролик")
    expect("у конкурентов берутся только залетевшие", outliers_only)

    def gaps_detected():
        own = conn.execute("SELECT id FROM accounts WHERE kind='own'").fetchone()["id"]
        conn.execute("DELETE FROM account_stats WHERE account_id = ?", (own,))
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        sync.write_account_day(conn, own, yesterday, {"reach": 10}, {}, {})
        gaps = sync.missing_days(conn, own, days=7)
        check(yesterday not in gaps, "записанный день попал в пропуски")
        check(len(gaps) == 6, f"пропусков {len(gaps)}, ожидалось 6")
        today = datetime.now(timezone.utc).date().isoformat()
        check(today not in gaps, "сегодняшний день не должен считаться пропуском — он ещё идёт")
    expect("пропуски в дневной статистике находятся", gaps_detected)

    def staleness():
        check(sync.is_stale(conn, hours=1) in (True, False), "проверка свежести не падает")
        conn.execute("UPDATE sync_jobs SET status='done', kind='own', finished_at=? WHERE id=1",
                     (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000"),))
        conn.commit()
        check(not sync.is_stale(conn, hours=12), "свежий прогон считается устаревшим")
        check(sync.is_stale(conn, hours=0), "нулевой порог должен считать данные устаревшими")
    expect("свежесть данных определяется", staleness)

    def units_are_normalised():
        # ровно тот ответ, что пришёл с боевого токена
        raw = {"reels_skip_rate": 27.4, "ig_reels_avg_watch_time": 5927,
               "ig_reels_video_view_total_time": 8_800_000, "views": 1485, "reach": 1247}
        out = sync.normalise_media_values(raw)
        check(abs(out["reels_skip_rate"] - 0.274) < 1e-9,
              f"проценты не переведены в долю: {out['reels_skip_rate']}")
        check(abs(out["ig_reels_avg_watch_time"] - 5.927) < 1e-9,
              f"миллисекунды не переведены в секунды: {out['ig_reels_avg_watch_time']}")
        check(abs(out["ig_reels_video_view_total_time"] - 8800.0) < 1e-9, "суммарное время не переведено")
        check(out["views"] == 1485 and out["reach"] == 1247, "счётчики не должны меняться")
        # доля уже в долях — не делим повторно
        again = sync.normalise_media_values(out)
        check(abs(again["reels_skip_rate"] - 0.274) < 1e-9, "долю поделили второй раз")

    expect("единицы API приводятся к каноническим", units_are_normalised)

    def hook_score_is_sane_after_normalisation():
        from app import metrics as _m
        out = sync.normalise_media_values({"reels_skip_rate": 27.4})
        score = _m.hook_score(out["reels_skip_rate"])
        check(0 < score < 1, f"сила хука вне диапазона: {score}")
        check(abs(score - 0.726) < 1e-9, f"сила хука {score}, ожидалось 0.726")
    expect("сила хука считается верно на боевых данных", hook_score_is_sane_after_normalisation)

    def job_log():
        rows = conn.execute("SELECT kind, status, api_calls_used FROM sync_jobs").fetchall()
        check(len(rows) == 3, f"прогонов записано {len(rows)}, ожидалось 3")
        check(any(r["api_calls_used"] > 0 for r in rows), "вызовы не посчитаны")
    expect("прогоны и потраченные вызовы залогированы", job_log)

    print(f"\n{'все тесты прошли' if not failures else str(len(failures)) + ' упало'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
