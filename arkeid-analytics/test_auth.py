"""Пароли и вход."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db


def main() -> int:
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    def hashing():
        stored = auth.hash_password("правильный-пароль")
        assert "правильный-пароль" not in stored, "пароль виден в хеше"
        assert stored.startswith("scrypt$")
        assert auth.verify_password("правильный-пароль", stored)
        assert not auth.verify_password("другой-пароль", stored)
    expect("пароль хранится хешем и проверяется", hashing)

    def salted():
        a = auth.hash_password("одинаковый")
        b = auth.hash_password("одинаковый")
        assert a != b, "одинаковые пароли дают одинаковый хеш — нет соли"
    expect("у каждого пароля своя соль", salted)

    def malformed():
        assert not auth.verify_password("x", "мусор")
        assert not auth.verify_password("x", "md5$aa$bb")
    expect("испорченный хеш не пропускает", malformed)

    tmp = Path(tempfile.mkdtemp()) / "auth.db"
    conn = db.connect(tmp)
    db.init(conn)

    def flow():
        assert not auth.any_users(conn)
        auth.create_user(conn, "Ivan", "очень-секретно", "Иван")
        conn.commit()
        assert auth.any_users(conn)
        assert auth.authenticate(conn, "ivan", "очень-секретно"), "логин нечувствителен к регистру"
        assert auth.authenticate(conn, "IVAN", "очень-секретно")
        assert not auth.authenticate(conn, "ivan", "не-тот-пароль")
        assert not auth.authenticate(conn, "нет-такого", "очень-секретно")
    expect("вход работает, регистр логина не важен", flow)

    def revoke():
        auth.set_active(conn, "ivan", False)
        conn.commit()
        assert not auth.authenticate(conn, "ivan", "очень-секретно"), "отключённый вошёл"
        auth.set_active(conn, "ivan", True)
        conn.commit()
        assert auth.authenticate(conn, "ivan", "очень-секретно")
    expect("отзыв доступа действует", revoke)

    print(f"\n{'все тесты прошли' if not failures else str(len(failures)) + ' упало'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
