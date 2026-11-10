"""Настройки аккаунта: что от них зависит и что переносится со старой установки.

Разрезы демографии здесь записаны ровно теми строками, которыми их отдаёт
Instagram: город — «Rostov-on-Don, Rostov Oblast», страна — «RU». Выдуманные
ключи уже однажды давали зелёный тест на коде, который на боевых данных
ничего не находил.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, geo, niches
from app import settings as account_settings
from app.web import queries as q

# Срез, как он приходит от API: город со областью через запятую, страна кодом.
SNAPSHOT = "2026-09-17"
CITIES = [("Rostov-on-Don, Rostov Oblast", 79), ("Volzhskiy, Volgograd Oblast", 43),
          ("Chita, Zabaykalsky Krai", 36), ("Moscow, Moscow", 17)]
COUNTRIES = [("RU", 307), ("UZ", 37), ("KZ", 21), ("UA", 20)]


SCHEMA = (Path(__file__).resolve().parent.parent / "app" / "schema.sql")


def fresh_db(with_own=True):
    """База для проверки.

    with_own=True — установка, которая работала до этой версии: схема без
    таблицы настроек и уже подключённый свой аккаунт. Именно её обновление
    должно сохранить прежние цифры.

    with_own=False — первый запуск на чистой машине: аккаунта ещё нет.
    """
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = db.connect(str(tmp))
    if with_own:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        # так база и выглядела до этой версии — настроек в ней не было вовсе
        conn.execute("DROP TABLE account_settings")
        conn.execute("INSERT INTO accounts (id, username, kind, followers_count)"
                     " VALUES (1, 'demo_detailing', 'own', 592)")
        conn.commit()
    db.init(conn)
    return conn


def fill_demographics(conn, account_id=1):
    for breakdown, rows in (("city", CITIES), ("country", COUNTRIES),
                            ("age", [("25-34", 232), ("18-24", 158)]),
                            ("gender", [("M", 324), ("F", 180)])):
        for key, value in rows:
            conn.execute(
                "INSERT OR REPLACE INTO demographics (account_id, date, metric,"
                " breakdown, value_key, value)"
                " VALUES (?, ?, 'follower_demographics', ?, ?, ?)",
                (account_id, SNAPSHOT, breakdown, key, value))
    conn.commit()


def main() -> int:
    failures = []

    def expect(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")

    def old_install_keeps_its_numbers():
        """Главная проверка обновления: установка, которая жила на константах,
        после переезда в настройки показывает ровно те же цифры."""
        conn = fresh_db()
        fill_demographics(conn)
        s = account_settings.load(conn, 1)
        assert s["city_key"] == "Rostov-on-Don", s
        assert s["country_code"] == "RU" and s["niche"] == "detailing", s
        assert s["can_reach"], "физический адрес потерялся при переносе"
        d = q.demographics(conn, 1)
        assert d["home_city"] == 79, d["home_city"]
        assert d["home_country"] == 307, d["home_country"]
        assert d["abroad"] == 592 - 307, d["abroad"]
        assert round(d["home_city_share"], 4) == 0.1334, d["home_city_share"]
    expect("старая установка сохраняет свои цифры", old_install_keeps_its_numbers)

    def new_install_gets_no_city():
        """Свежая установка не наследует чужой город.

        Проверяем именно тот порядок, в котором это происходит на новой машине:
        сначала пустая база, потом подключение своего аккаунта, потом очередной
        запрос — а init() выполняется на каждый. Если перенос смотрит только на
        «настроек нет, а аккаунт есть», клиент получит Ростов ровно здесь.
        """
        conn = fresh_db(with_own=False)
        conn.execute("INSERT INTO accounts (id, username, kind, followers_count)"
                     " VALUES (1, 'someone', 'own', 592)")
        conn.commit()
        db.init(conn)
        s = account_settings.load(conn, 1)
        assert s["city_key"] == "" and s["country_code"] == "", s
        assert not s["can_reach"] and s["niche"] == "", s
    expect("новой установке чужой город не достаётся", new_install_gets_no_city)

    def carrying_over_happens_once():
        """Перенос — разовый. Иначе он затирал бы то, что клиент поправил руками."""
        conn = fresh_db()
        account_settings.save(conn, 1, dict(account_settings.LEGACY,
                                            city_key="Moscow", city_name="Москва",
                                            city_in="в Москве"))
        db.migrate(conn, settings_table_is_new=True)
        assert account_settings.load(conn, 1)["city_key"] == "Moscow", \
            "повторный перенос вернул старую константу поверх выбора клиента"
    expect("перенос констант срабатывает один раз", carrying_over_happens_once)

    def city_comes_from_settings():
        """Сменили город — сменился и счёт. Раньше он был вшит в код."""
        conn = fresh_db()
        fill_demographics(conn)
        account_settings.save(conn, 1, {"niche": "detailing", "has_address": True,
                                        "city_key": "Chita", "city_name": "Чита",
                                        "city_in": "в Чите", "country_code": "RU"})
        d = q.demographics(conn, 1)
        assert d["home_city"] == 36, d["home_city"]
        assert d["home_city_in"] == "в Чите", d["home_city_in"]
    expect("город берётся из настроек, а не из кода", city_comes_from_settings)

    def blogger_has_no_reach_block():
        """У блогера физического адреса нет: «доехать могут 13%» — не вывод,
        а повод чинить то, что не сломано."""
        conn = fresh_db()
        fill_demographics(conn)
        account_settings.save(conn, 1, {"niche": "", "has_address": False,
                                        "city_key": "Rostov-on-Don",
                                        "city_name": "Ростов-на-Дону",
                                        "city_in": "в Ростове-на-Дону",
                                        "country_code": "RU"})
        d = q.demographics(conn, 1)
        assert not d["can_reach"], "блок досягаемости остался без адреса"
        # сам город при этом считается: знать состав аудитории полезно и блогеру
        assert d["home_city"] == 79, d["home_city"]
        steps = q.next_steps(conn, 1, {})
        assert not any(s["q"] == "Кто вообще на нас подписан?" for s in steps), \
            "подсказка про клиентов показана аккаунту без адреса"
    expect("без адреса досягаемость не считается", blogger_has_no_reach_block)

    def unset_city_gives_no_share():
        """Города нет — доли нет: ноль читался бы как «в городе никого»."""
        conn = fresh_db()
        fill_demographics(conn)
        account_settings.save(conn, 1, {"country_code": "KZ"})
        d = q.demographics(conn, 1)
        assert d["home_city_share"] is None, d["home_city_share"]
        # страна тоже из настроек: своей теперь считается казахстанская доля
        assert d["home_country"] == 21, d["home_country"]
        assert d["abroad"] == 592 - 21, d["abroad"]
        assert d["home_country_in"] == "в Казахстане", d["home_country_in"]
        assert "Казахстан" not in d["abroad_top"], d["abroad_top"]
    expect("без города доля не показывается", unset_city_gives_no_share)

    def form_junk_is_rejected():
        """В форму приходит что угодно: ниша, которой нет в поставке, и код
        страны из головы. И то и другое молча сломало бы экраны."""
        conn = fresh_db()
        s = account_settings.save(conn, 1, {"niche": "../../etc/passwd",
                                            "country_code": "зз",
                                            "city_key": "  Chita  "})
        assert s["niche"] == "", s["niche"]
        assert s["country_code"] == "", s["country_code"]
        assert s["city_key"] == "Chita", s["city_key"]
    expect("мусор из формы не сохраняется", form_junk_is_rejected)

    def city_case_falls_back():
        """Предложный падеж необязателен: не заполнили — берём именительный."""
        conn = fresh_db()
        s = account_settings.save(conn, 1, {"city_key": "Kazan", "city_name": "Казань"})
        assert s["city_in"] == "Казань", s["city_in"]
    expect("падеж города необязателен", city_case_falls_back)

    def country_cases_are_right():
        """Предлог меняется вместе со страной, и склонять его кодом нельзя."""
        assert geo.in_country("RU") == "в России", geo.in_country("RU")
        assert geo.in_country("CY") == "на Кипре", geo.in_country("CY")
        assert geo.in_country("FR") == "во Франции", geo.in_country("FR")
        assert geo.of_country("KZ") == "Казахстана", geo.of_country("KZ")
        # незнакомый код не выдумываем — показываем как есть
        assert geo.name("ZZ") == "ZZ" and geo.of_country("ZZ") == "ZZ"
    expect("падежи стран на месте", country_cases_are_right)

    def every_country_has_all_forms():
        """Пустая форма означала бы пустое место в предложении на экране."""
        for code, forms in geo.COUNTRIES.items():
            assert len(forms) == 3 and all(forms), f"{code}: {forms}"
            assert forms[1].startswith(("в ", "во ", "на ")), f"{code}: нет предлога"
    expect("у каждой страны три заполненные формы", every_country_has_all_forms)

    def niche_pack_is_reachable():
        conn = fresh_db()
        account_settings.save(conn, 1, {"niche": "detailing"})
        pack = account_settings.niche_pack(conn, 1)
        assert pack and pack["key"] == "detailing", pack
        assert account_settings.load(conn, 1)["niche_name"] == "Детейлинг"
        assert niches.load("detailing") is not None
    expect("ниша из настроек находит свою пачку", niche_pack_is_reachable)

    if failures:
        print(f"\nпровалено: {len(failures)}")
        return 1
    print("\nвсе тесты прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
