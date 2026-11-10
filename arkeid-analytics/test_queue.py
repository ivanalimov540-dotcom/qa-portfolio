"""Очередь расшифровок: брошенные задачи возвращаются в работу."""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.transcribe import worker


def stamp(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


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
    conn.execute("INSERT INTO accounts (id, username, kind) VALUES (1,'a','own')")
    for i in range(1, 5):
        conn.execute(
            "INSERT INTO media (id, account_id, ig_media_id, media_type) VALUES (?,1,?,'REELS')",
            (i, f"m{i}"))

    def add(media_id, status, attempts, minutes_ago):
        conn.execute(
            "INSERT INTO transcripts (media_id, status, attempts, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (media_id, status, attempts, stamp(minutes_ago), stamp(minutes_ago)))

    add(1, "running", 1, 120)   # брошена час назад — вернуть в очередь
    add(2, "running", 0, 2)     # только что взята — не трогать
    add(3, "running", 3, 120)   # брошена и попытки исчерпаны — закрыть
    add(4, "done", 0, 500)      # готовую не трогаем
    conn.commit()

    moved = worker.reclaim_stalled(conn)

    def counted():
        assert moved == 2, f"подобрано {moved}, ожидалось 2"
    expect("подобраны только просроченные", counted)

    def back_in_queue():
        row = conn.execute("SELECT status FROM transcripts WHERE media_id=1").fetchone()
        assert row["status"] == "pending", f"статус {row['status']}, ожидался pending"
    expect("брошенная задача снова в очереди", back_in_queue)

    def running_now_untouched():
        row = conn.execute("SELECT status FROM transcripts WHERE media_id=2").fetchone()
        assert row["status"] == "running", "задача идущего прогона не должна отбираться"
    expect("текущая задача не отбирается", running_now_untouched)

    def hopeless_closed():
        row = conn.execute("SELECT status FROM transcripts WHERE media_id=3").fetchone()
        assert row["status"] == "error", f"статус {row['status']}, ожидался error"
    expect("исчерпавшая попытки закрывается, а не крутится", hopeless_closed)

    def done_untouched():
        row = conn.execute("SELECT status FROM transcripts WHERE media_id=4").fetchone()
        assert row["status"] == "done", "готовую расшифровку трогать нельзя"
    expect("готовая расшифровка не трогается", done_untouched)

    def claimable():
        task = worker.claim_next(conn)
        assert task is not None, "подобранная задача не выдаётся в работу"
        assert task["media_id"] == 1, f"выдана задача {task['media_id']}"
    expect("подобранную задачу можно взять в работу", claimable)

    def file_dropped_even_when_not_downloaded_now():
        """Файл от прошлого прогона тоже должен убираться."""
        import tempfile as _t
        f = Path(_t.mkdtemp()) / "v.mp4"
        f.write_bytes(b"x" * 10)
        conn.execute("INSERT INTO media (id, account_id, ig_media_id, media_type)"
                     " VALUES (5,1,'m5','REELS')")
        conn.execute(
            "INSERT INTO media_files (media_id, path, bytes, downloaded_at)"
            " VALUES (5, ?, 10, ?)", (str(f), stamp(0)))
        conn.commit()
        worker.drop_file(conn, 5, f)
        assert not f.exists(), "видео осталось на диске"
        row = conn.execute(
            "SELECT deleted_at FROM media_files WHERE media_id=5").fetchone()
        assert row["deleted_at"], "запись о файле не закрыта"
    expect("видео удаляется, кем бы оно ни было скачано", file_dropped_even_when_not_downloaded_now)

    def cleanup_sits_in_finally():
        """Уборка не должна зависеть от того, чем кончилась расшифровка.

        Проверяем текстом: именно перенос drop_file на удачную ветку и оставил
        три файла на диске. Тест на поведении здесь потребовал бы поднимать
        Whisper, поэтому сторожим структуру.
        """
        src = (Path(__file__).resolve().parent.parent
               / "app" / "transcribe" / "worker.py").read_text()
        body = src[src.index("def process_one("):]
        tail = body[body.index("finally:"):]
        assert "drop_file" in tail, "drop_file не в finally — уборка снова условная"
        head = body[:body.index("finally:")]
        assert "if downloaded" not in head, "уборка снова привязана к скачиванию"
    expect("уборка стоит в finally, а не на удачной ветке", cleanup_sits_in_finally)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
