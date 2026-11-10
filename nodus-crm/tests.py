"""Проверки денег (те же примеры, что в общей таблице), рассылки и доступов."""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import outreach
from .models import Company, Lead, Partner, PayerType, Payment, Profile, Service, Source
from .money import owner_checks, person_year


def mk(name, owner=False):
    u = User.objects.create_user(name.lower(), password="test-pass-123", first_name=name)
    Profile.objects.create(user=u, is_owner=owner)
    return u


class MoneyTests(TestCase):
    def setUp(self):
        self.ivan = mk("Иван", owner=True)
        self.rafik = mk("Рафик")
        self.denis = mk("Денис")
        self.kostya = mk("Костя")
        self.company = PayerType.objects.get(name="Компания или ИП")
        self.person = PayerType.objects.get(name="Частное лицо")
        self.src = {s.name: s for s in Source.objects.all()}

    def lead(self, **kw):
        return Lead.objects.create(client="Тест", created_by=self.ivan, **kw)

    def pay(self, lead, amount, day=date(2026, 11, 5), **kw):
        return Payment.objects.create(lead=lead, amount=amount, paid_on=day, **kw)

    def test_self_found_company(self):
        lead = self.lead(source=self.src["Сам нашёл"], finder=self.denis, doer=self.denis, payer=self.company, price=40000)
        p = self.pay(lead, 20000)
        self.assertEqual((p.tax, p.net, p.studio, p.doer_share, p.finder_share), (Decimal("1200"), Decimal("18800"), Decimal("9400"), Decimal("9400"), Decimal("0")))
        self.assertEqual(p.receiver, self.denis)

    def test_passed_private(self):
        lead = self.lead(source=self.src["Сам нашёл"], finder=self.kostya, doer=self.rafik, payer=self.person)
        p = self.pay(lead, 25000)
        self.assertEqual(p.scheme_code, "passed")
        self.assertEqual((p.tax, p.studio, p.doer_share, p.finder_share), (Decimal("1000"), Decimal("12000"), Decimal("9600"), Decimal("2400")))

    def test_platform_no_tax(self):
        lead = self.lead(source=self.src["Площадка"], doer=self.rafik, payer=self.person)
        p = self.pay(lead, 20000)
        self.assertEqual((p.tax, p.studio, p.doer_share), (Decimal("0"), Decimal("8800"), Decimal("11200")))

    def test_referral_studio(self):
        lead = self.lead(source=self.src["Сарафан"], doer=self.kostya, receiver=self.ivan, payer=self.person, referrer="Маша")
        p = self.pay(lead, 15000, day=date(2026, 12, 1))
        self.assertEqual((p.tax, p.studio, p.doer_share, p.referral), (Decimal("600"), Decimal("7200"), Decimal("7200"), Decimal("1500")))
        self.assertEqual(p.receiver, self.ivan)

    def test_partner_finder_is_partner_owner(self):
        partner = Partner.objects.create(name="Таргетолог Оля", owner=self.kostya)
        lead = self.lead(source=self.src["Партнёр"], partner=partner, doer=self.denis, payer=self.company)
        self.assertEqual(lead.finder, self.kostya)
        p = self.pay(lead, 25000)
        self.assertEqual((p.scheme_code, p.tax, p.studio, p.doer_share, p.finder_share, p.referral),
                         ("passed", Decimal("1500"), Decimal("11750"), Decimal("9400"), Decimal("2350"), Decimal("2500")))

    def test_lead_change_recalculates_payments(self):
        lead = self.lead(source=self.src["Сам нашёл"], finder=self.denis, doer=self.denis, payer=self.company)
        p = self.pay(lead, 20000)
        lead.payer = self.person
        lead.save()
        p.refresh_from_db()
        self.assertEqual(p.tax, Decimal("800"))

    def test_person_year_matches_table(self):
        # те же шесть оплат, что в тестовой таблице 05.11
        l1 = self.lead(source=self.src["Сам нашёл"], finder=self.denis, doer=self.denis, payer=self.company)
        l2 = self.lead(source=self.src["Сам нашёл"], finder=self.kostya, doer=self.rafik, payer=self.person)
        l3 = self.lead(source=self.src["Площадка"], doer=self.rafik, payer=self.person)
        l4 = self.lead(source=self.src["Сарафан"], doer=self.kostya, receiver=self.ivan, payer=self.person)
        l5 = self.lead(source=self.src["Сайт"], doer=self.denis, payer=self.company)
        self.pay(l1, 20000, studio_transferred=True)
        self.pay(l1, 20000, day=date(2026, 11, 20))
        self.pay(l2, 25000, day=date(2026, 11, 10))
        self.pay(l3, 20000, day=date(2026, 11, 12))
        self.pay(l4, 15000, day=date(2026, 12, 1))
        self.pay(l5, 45000, day=date(2026, 11, 15))
        iv = person_year(self.ivan, 2026)
        self.assertEqual((iv["from_clients"], iv["from_team"], iv["own"]), (Decimal("15000"), Decimal("60750"), Decimal("66450")))
        dn = person_year(self.denis, 2026)
        self.assertEqual((dn["from_clients"], dn["own"], dn["owes_owner"]), (Decimal("85000"), Decimal("39950"), Decimal("30550")))
        rf = person_year(self.rafik, 2026)
        self.assertEqual((rf["from_clients"], rf["own"], rf["owes_owner"]), (Decimal("45000"), Decimal("20800"), Decimal("20800")))
        ks = person_year(self.kostya, 2026)
        self.assertEqual((ks["from_team"], ks["own"], ks["owed_to"]), (Decimal("9600"), Decimal("9600"), Decimal("9600")))
        checks = {(c["month"], c["payer"].first_name): c["total"] for c in owner_checks(self.ivan)}
        self.assertEqual(checks[(date(2026, 11, 1), "Денис")], Decimal("39950"))
        self.assertEqual(checks[(date(2026, 11, 1), "Рафик")], Decimal("20800"))

    def test_below_price(self):
        shop = Service.objects.get(name="Интернет-магазин")
        lead = self.lead(source=self.src["Сайт"], service=shop, price=90000)
        self.assertTrue(lead.below_price)


class OutreachTests(TestCase):
    def setUp(self):
        self.ivan = mk("Иван", owner=True)
        self.kostya = mk("Костя")
        self.denis = mk("Денис")

    def test_duplicates_by_phone_site_tg(self):
        Company.objects.create(name="Улыбка", city="Казань", phone="+7 (900) 111-22-33", site="https://www.ulybka.ru/", telegram="@ulybka")
        self.assertTrue(Company(name="Другое", phone="89001112233").duplicates().exists())
        self.assertTrue(Company(name="Другое", site="ulybka.ru").duplicates().exists())
        self.assertTrue(Company(name="Другое", telegram="https://t.me/Ulybka").duplicates().exists())
        self.assertTrue(Company(name="«Улыбка»", city="Казань").duplicates().exists())
        self.assertFalse(Company(name="Улыбка", city="Самара").duplicates().exists())

    def test_send_flow_and_followup(self):
        c = Company.objects.create(name="Кафе", city="Самара", phone="89001234567")
        outreach.take(c, self.kostya)
        self.assertEqual(c.status, "taken")
        self.assertFalse(c.can_write(self.denis))
        outreach.mark_sent(c, self.kostya, "wa", "first")
        self.assertEqual((c.status, c.step, c.next_touch_on), ("sent", 1, timezone.localdate() + timedelta(days=3)))
        links = outreach.send_links(c, self.kostya)
        self.assertEqual(links["kind"], "followup")
        self.assertTrue(links["links"][0]["url"].startswith("https://wa.me/79001234567?text="))
        outreach.mark_sent(c, self.kostya, "wa", "followup")
        self.assertEqual(c.status, "followup")
        Company.objects.filter(pk=c.pk).update(last_touch_at=timezone.now() - timedelta(days=5))
        outreach.refresh_statuses()
        c.refresh_from_db()
        self.assertEqual((c.status, c.assigned_to), ("no_reply", None))

    def test_convert_to_lead(self):
        c = Company.objects.create(name="Автосервис", city="Казань")
        outreach.take(c, self.kostya)
        lead = outreach.convert_to_lead(c, self.kostya)
        self.assertEqual((lead.source.name, lead.finder, lead.stage), ("Рассылка", self.kostya, "contacted"))

    def test_import_cards_json(self):
        Company.objects.create(name="Старая", city="Казань", external_id="2gis-1")
        payload = {
            "denis": [{"did": "2gis-2", "name": "Re:Market", "city": "Новосибирск", "tg": "https://t.me/egor", "body": "Текст", "ch": "tg"}],
            "ivan_rem": [],
            "left": [{"did": "2gis-1", "name": "Старая", "city": "Казань"}, {"did": "2gis-3", "name": "Новая", "city": "Самара", "wa": "https://wa.me/79005556677"}],
        }
        rows = outreach.classify(outreach.parse_upload("cards_0411.json", json.dumps(payload).encode()))
        self.assertEqual([bool(r["dup"]) for r in rows], [False, True, False])
        n = outreach.import_rows(rows, self.ivan, "тест", {"denis": self.denis})
        self.assertEqual(n, 2)
        c = Company.objects.get(external_id="2gis-2")
        self.assertEqual((c.assigned_to, c.status, c.message), (self.denis, "taken", "Текст"))
        self.assertEqual(Company.objects.get(external_id="2gis-3").wa_number, "79005556677")


class AccessTests(TestCase):
    def setUp(self):
        self.ivan = mk("Иван", owner=True)
        self.kostya = mk("Костя")
        self.denis = mk("Денис")
        src = Source.objects.get(name="Сам нашёл")
        self.mine = Lead.objects.create(client="Мой", source=src, finder=self.kostya, doer=self.kostya, created_by=self.kostya)
        self.other = Lead.objects.create(client="Чужой", source=src, finder=self.denis, doer=self.denis, created_by=self.denis)

    def test_member_sees_only_own_leads(self):
        self.assertEqual(list(Lead.visible_to(self.kostya)), [self.mine])
        self.assertEqual(Lead.visible_to(self.ivan).count(), 2)

    def test_member_cannot_open_foreign_lead(self):
        self.client.login(username="костя", password="test-pass-123")
        r = self.client.get(reverse("lead", args=[self.other.number]))
        self.assertEqual(r.status_code, 404)
        r = self.client.get(reverse("lead", args=[self.mine.number]))
        self.assertEqual(r.status_code, 200)

    def test_settings_owner_only(self):
        self.client.login(username="костя", password="test-pass-123")
        self.assertEqual(self.client.get(reverse("settings")).status_code, 403)
        self.client.login(username="иван", password="test-pass-123")
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)


class ImportViewTests(TestCase):
    def setUp(self):
        self.ivan = mk("Иван", owner=True)
        self.denis = mk("Денис")
        self.client.login(username="иван", password="test-pass-123")

    def test_upload_preview_and_confirm(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        payload = {"denis": [{"did": "2gis-9", "name": "Кофейня", "city": "Казань", "wa": "https://wa.me/79001112233", "body": "Текст"}],
                   "left": [{"did": "2gis-10", "name": "Пекарня", "city": "Казань"}]}
        f = SimpleUploadedFile("cards_0511.json", json.dumps(payload).encode(), content_type="application/json")
        r = self.client.post(reverse("outreach_import"), {"file": f, "batch": "Тест"})
        self.assertContains(r, "Загрузить 2 компании")
        token = r.context["preview"]["token"]
        r = self.client.post(reverse("outreach_import"), {"token": token, "who_denis": self.denis.pk, "assign": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Company.objects.get(external_id="2gis-9").assigned_to, self.denis)
        self.assertEqual(Company.objects.get(external_id="2gis-10").status, "free")

    def test_pages_open(self):
        for name in ("home", "outreach", "outreach_import", "leads", "money", "partners", "settings", "lead_new", "company_new"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)


class SearchTests(TestCase):
    def setUp(self):
        self.ivan = mk("Иван", owner=True)
        self.client.login(username="иван", password="test-pass-123")

    def test_cyrillic_case_insensitive_and_phone(self):
        Company.objects.create(name="Белая линия", city="Казань", phone="+7 (904) 228-78-93")
        r = self.client.get(reverse("search"), {"q": "бел"}).json()
        self.assertEqual(r["results"][0]["title"], "Белая линия")
        r = self.client.get(reverse("search"), {"q": "8 904 228 78 93"}).json()
        self.assertEqual(len(r["results"]), 1)
        r = self.client.get(reverse("outreach"), {"tab": "all", "q": "КАЗАНЬ"})
        self.assertContains(r, "Белая линия")


class ScoringTests(TestCase):
    """Отбор по решениям 05.11: отсекают только исключения, баллы ставят в очередь."""

    def test_exclusions(self):
        from .harvest import score
        self.assertEqual(score(Company(name="Ноготки", niche="Ногтевая студия"))[0], -1)
        self.assertEqual(score(Company(name="Мало", niche="Стоматология", reviews=12, rating=Decimal("4.9")))[0], -1)
        self.assertEqual(score(Company(name="Плохая", niche="Стоматология", rating=Decimal("4.3"), reviews=80))[0], -1)
        self.assertIn("федеральная", score(Company(name="DNS", niche="Магазин электроники"))[1])
        self.assertIn("федеральная", score(Company(name="Сеть", niche="Кафе", branches=120))[1])
        self.assertGreaterEqual(score(Company(name="Море рыбы", niche="Рыба", branches=13, reviews=200))[0], 1)  # местная сеть
        self.assertGreaterEqual(score(Company(name="Салон", niche="Салон красоты", reviews=60, rating=Decimal("4.8")))[0], 0)
        self.assertEqual(score(Company(name="Автоателье", niche="Автосервис", reviews=40))[0], 1)  # «ателье» внутри слова — не стоп

    def test_points(self):
        from .harvest import score
        pts, why = score(Company(name="Бар", niche="Бары", reviews=450, ads=True, price_max=12000, branches=2))
        self.assertEqual(pts, 2 + 2 + 1 + 1 + 1)  # отзывы, реклама, цены, точки, сайта нет
        self.assertIn("реклама на картах +2", why)
        self.assertEqual(score(Company(name="Сайт упал", niche="Бары", site="x.ru", hook="site_down"))[0], 2)

    def test_reviews_relative_to_niche_in_city(self):
        from .harvest import score
        for i, n in enumerate([5, 10, 15, 20, 25, 30, 35]):
            Company.objects.create(name=f"Клиника {i}", niche="Клиника", city="Ростов-на-Дону", reviews=n, phone=f"+7900000000{i}")
        gino = Company(name="ГИНО", niche="Клиника", city="Ростов-на-Дону", reviews=44)
        self.assertIn("больше, чем у 3/4 ниши", score(gino)[1])

    def test_niche_weights_from_replies(self):
        from .harvest import niche_weights
        for i in range(160):
            Company.objects.create(name=f"Тел {i}", niche="Мобильные телефоны", status="replied" if i < 8 else "no_reply", phone=f"+79100000{i:03d}")
            Company.objects.create(name=f"Дет {i}", niche="Детейлинг", status="no_reply", phone=f"+79200000{i:03d}")
            Company.objects.create(name=f"Бар {i}", niche="Бары", status="replied" if i < 2 else "no_reply", phone=f"+79300000{i:03d}")
        w = niche_weights()
        self.assertEqual((w["мобильные телефоны"], w["детейлинг"]), (2, -1))
        self.assertNotIn("бары", w)  # 2 ответа — мало, чтобы судить

    def test_round_rating_and_note(self):
        from .harvest import parse_note
        self.assertEqual(parse_note("2ГИС (19.10): 5, 1 159 отзывов"), (Decimal("5.0"), 1159))
        self.assertEqual(parse_note("Яндекс 4,8 / 804 отзыва"), (Decimal("4.8"), 804))


class TelegramTests(TestCase):
    def setUp(self):
        from . import telegram
        from .models import StudioSettings
        self.tg = telegram
        self.sent = []
        self.old = telegram.transport
        telegram.transport = lambda token, method, params, timeout: (self.sent.append((method, params)) or {"ok": True, "result": {"username": "nodus_crm_bot"}})
        st = StudioSettings.get()
        st.tg_token, st.tg_bot_username = "123:abc", "nodus_crm_bot"
        st.save()
        self.ivan = mk("Иван", owner=True)
        self.denis = mk("Денис")

    def tearDown(self):
        self.tg.transport = self.old

    def test_link_and_today(self):
        code = self.tg.link_code(self.denis)
        self.tg.handle_update({"message": {"chat": {"id": 555}, "text": f"/start {code}"}})
        self.denis.profile.refresh_from_db()
        self.assertEqual(self.denis.profile.tg_chat_id, 555)
        self.assertIn("Готово, Денис", self.sent[-1][1]["text"])
        self.tg.handle_update({"message": {"chat": {"id": 555}, "text": "Сегодня"}})
        self.assertIn("дела на", self.sent[-1][1]["text"])
        self.tg.handle_update({"message": {"chat": {"id": 999}, "text": "привет"}})
        self.assertIn("не знаю", self.sent[-1][1]["text"])

    def test_bot_loop_once(self):
        from io import StringIO
        from django.core.management import call_command
        code = self.tg.link_code(self.denis)

        def transport(token, method, params, timeout):
            self.sent.append((method, params))
            if method == "getUpdates":
                return {"ok": True, "result": [{"update_id": 7, "message": {"chat": {"id": 555}, "text": f"/start {code}"}}]}
            return {"ok": True, "result": {"username": "nodus_crm_bot"} if method == "getMe" else True}
        self.tg.transport = transport
        call_command("tgbot", once=True, stdout=StringIO(), stderr=StringIO())
        self.assertIn("setMyCommands", [m for m, _ in self.sent])
        self.denis.profile.refresh_from_db()
        self.assertEqual(self.denis.profile.tg_chat_id, 555)

    def test_payment_notifies_doer_and_owner(self):
        from .models import PayerType, Profile, TgMessage
        Profile.objects.filter(user__in=[self.ivan, self.denis]).update(tg_chat_id=1)
        Profile.objects.filter(user=self.denis).update(tg_chat_id=2)
        lead = Lead.objects.create(client="Улыбка", source=Source.objects.get(name="Сайт"), doer=self.denis,
                                   payer=PayerType.objects.get(name="Компания или ИП"), created_by=self.ivan)
        self.client.login(username="иван", password="test-pass-123")
        self.client.post(reverse("payment_add", args=[lead.number]), {"paid_on": "2026-11-05", "amount": "20000"})
        texts = list(TgMessage.objects.values_list("user__first_name", "text"))
        self.assertEqual(len(texts), 1)  # Иван сам записал — ему не шлём
        self.assertEqual(texts[0][0], "Денис")
        self.assertIn("Твоя доля — 9 400 ₽", texts[0][1])
        self.assertEqual(self.tg.send_outbox(), 1)
        self.assertEqual(self.sent[-1][1]["chat_id"], 2)

    def test_digest_once_a_day(self):
        from datetime import datetime
        from .models import Profile, TgMessage
        Profile.objects.filter(user=self.denis).update(tg_chat_id=2)
        at = timezone.make_aware(datetime(2026, 11, 6, 10, 0))
        self.assertEqual(self.tg.queue_digests(at), 1)
        self.assertEqual(self.tg.queue_digests(at), 0)
        self.assertEqual(TgMessage.objects.count(), 1)


class LeadEditTests(TestCase):
    def test_stage_change_through_form_is_recorded(self):
        ivan = mk("Иван", owner=True)
        self.client.login(username="иван", password="test-pass-123")
        lead = Lead.objects.create(client="Тест", source=Source.objects.get(name="Сайт"), doer=ivan, created_by=ivan,
                                   next_step="Позвонить", next_step_on=date(2026, 11, 10))
        before = lead.stage_changed_at
        data = {"client": "Тест", "source": lead.source_id, "doer": ivan.pk, "stage": "brief", "next_step": "Бриф",
                "next_step_on": "2026-11-11", "created_on": "2026-11-05"}
        r = self.client.post(reverse("lead_edit", args=[lead.number]), data)
        self.assertEqual(r.status_code, 302)
        lead.refresh_from_db()
        self.assertEqual(lead.stage, "brief")
        self.assertGreater(lead.stage_changed_at, before)
        self.assertTrue(lead.events.filter(text="Этап: Бриф").exists())


class BotHelpers(TestCase):
    """Бот: рассылка с телефона, ответы компаний, поиск, заявка одной строкой, кнопки сводки, отчёты, бизнес-чаты."""

    def setUp(self):
        from . import telegram
        from .models import StudioSettings
        self.tg = telegram
        self.sent = []
        self.old = telegram.transport

        def transport(token, method, params, timeout):
            self.sent.append((method, params))
            return {"ok": True, "result": {"message_id": len(self.sent)} if method == "sendMessage" else True}
        telegram.transport = transport
        st = StudioSettings.get()
        st.tg_token, st.tg_bot_username = "123:abc", "nodus_crm_bot"
        st.save()
        self.ivan = mk("Иван", owner=True)
        self.denis = mk("Денис")
        Profile.objects.filter(user=self.ivan).update(tg_chat_id=111)
        Profile.objects.filter(user=self.denis).update(tg_chat_id=555)
        Company.objects.all().delete()

    def tearDown(self):
        self.tg.transport = self.old

    def say(self, text, chat=555, reply_to=None):
        msg = {"chat": {"id": chat, "type": "private"}, "from": {"id": chat}, "text": text}
        if reply_to:
            msg["reply_to_message"] = {"from": {"is_bot": True}, "text": reply_to}
        self.tg.handle_update({"message": msg})

    def press(self, data, chat=555, text="", markup=None):
        self.tg.handle_update({"callback_query": {"id": "1", "from": {"id": chat}, "data": data, "message": {
            "message_id": 7, "chat": {"id": chat}, "text": text, "reply_markup": markup or {}}}})

    def last(self, method="sendMessage"):
        return [p for m, p in self.sent if m == method][-1]

    def buttons(self, params):
        return [b for row in params["reply_markup"]["inline_keyboard"] for b in row]

    def company(self, name, phone, score=3, **kw):
        return Company.objects.create(name=name, city="Казань", niche="Детейлинг", phone=phone, score=score, **kw)


class ExpertTests(BotHelpers):
    """Эксперты из Telegram и Instagram: свои тексты, канал Instagram, первыми до дневного предела."""

    def expert(self, name, nick, **kw):
        kw.setdefault("hook", "expert")
        kw.setdefault("niche", "Психолог")
        kw.setdefault("telegram", f"@{nick}")
        return Company.objects.create(name=name, score=0, **kw)

    def test_experts_first_until_daily_cap(self):
        from .models import StudioSettings
        st = StudioSettings.get()
        st.expert_daily_cap = 1
        st.save()
        self.company("Блеск", "+7 900 111-22-33", score=5)
        anna = self.expert("Анна", "anna_psy")
        self.expert("Ольга", "olga_coach")
        self.say("Новые")
        card = self.last()
        self.assertIn("Анна", card["text"])
        self.assertIn("страницу о вас с услугами и отзывами, а к ней бота", card["text"])
        self.press(f"os:{anna.pk}:tg:f")
        self.assertIn("Блеск", self.last()["text"])  # предел экспертов на сегодня набран

    def test_instagram_channel(self):
        maria = self.expert("Мария", "", telegram="", instagram="maria.nutri", niche="Нутрициолог")
        self.say("Новые")
        card = self.last()
        self.assertIn("https://ig.me/m/maria.nutri", [b.get("url", "") for b in self.buttons(card)])
        self.assertIn("программу или марафон", card["text"])
        self.press(f"os:{maria.pk}:ig:f")
        self.assertEqual(maria.touches.get(kind="first").channel, "ig")
        self.assertEqual(outreach.send_links(maria, self.denis)["links"][0]["url"], "https://ig.me/m/maria.nutri")

    def test_texts_by_hook(self):
        from .bot import reply_text
        site = self.expert("Анна", "anna_psy", hook="expert_site", hook_text="только по телефону", site="anna-psy.ru")
        self.assertIn("записаться к вам можно только по телефону", outreach.prepare(site, self.denis, "first")[0])
        self.assertIn("Ассистент, который отвечает на вопросы", reply_text(site, self.denis, "price"))
        biz = self.company("Блеск", "+7 900 111-22-33")
        self.assertIn("От 25 000", reply_text(biz, self.denis, "price"))
        # фраза под нишу: частные случаи раньше общих, экспертам — свои
        for niche, phrase in [("Барбершоп", "с услугами и ценами"), ("Бары", "с меню и фото"),
                              ("Мебель для кухни", "с вашими работами и ценами"), ("Мебель", "с каталогом и ценами"),
                              ("Ремонт техники", "с услугами и ценами"), ("Бытовая техника", "с каталогом и ценами"),
                              ("Пилатес и фитнес", "с расписанием и ценами"), ("Автосалон", "с автомобилями и ценами"),
                              ("Караоке-залы", "с меню и фото"), ("Антикафе", "с ценами и фото"),
                              ("Юридические услуги", "с услугами и ценой консультации")]:
            self.assertEqual(outreach.niche_phrase(niche), phrase, niche)
        self.assertEqual(outreach.niche_phrase("Фитнес-тренер", expert=True), "страницу под вашу программу или марафон")


class MistakeTests(BotHelpers):
    """Нажал «Отправил» по ошибке — вернуть; номера нет в WhatsApp или ник — канал — убрать контакт."""

    def test_unsend_in_bot_restores_card(self):
        c = self.company("Блеск", "+7 900 111-22-33")
        self.say("Новые")
        self.press(f"os:{c.pk}:wa:f")
        c.refresh_from_db()
        self.assertEqual(c.status, "sent")
        undo = [b for b in self.buttons(self.last("editMessageReplyMarkup")) if b.get("callback_data", "").startswith("us:")]
        self.assertEqual(len(undo), 1)
        self.press(undo[0]["callback_data"])
        c.refresh_from_db()
        self.assertEqual((c.status, c.step, c.first_sent_at), ("taken", 0, None))
        self.assertFalse(c.touches.filter(kind="first").exists())  # не идёт в норму
        restored = self.last("editMessageReplyMarkup")
        self.assertTrue(any(b.get("url", "").startswith("https://wa.me/") for b in self.buttons(restored)))

    def test_unsend_only_own_and_today(self):
        c = self.company("Блеск", "+7 900 111-22-33")
        outreach.take(c, self.denis)
        outreach.mark_sent(c, self.denis, "wa", "first")
        self.assertFalse(outreach.undo_sent(c, self.ivan))
        c.touches.filter(kind="first").update(created_at=timezone.now() - timedelta(days=1))
        self.assertFalse(outreach.undo_sent(c, self.denis))

    def test_unsend_in_crm(self):
        c = self.company("Блеск", "+7 900 111-22-33")
        outreach.take(c, self.denis)
        outreach.mark_sent(c, self.denis, "wa", "first")
        self.client.force_login(self.denis)
        self.assertIn("Не отправил — вернуть", self.client.get(reverse("company", args=[c.pk])).content.decode())
        self.client.post(reverse("company_action", args=[c.pk, "unsend"]))
        c.refresh_from_db()
        self.assertEqual(c.status, "taken")

    def test_dead_whatsapp_and_channel(self):
        c = self.company("Блеск", "+7 900 111-22-33", telegram="@blesk_channel")
        self.say("Новые")
        self.press(f"nc:{c.pk}:wa:f")
        c.refresh_from_db()
        self.assertEqual((c.wa_source, c.wa_number), ("none", ""))
        self.assertEqual(c.status, "taken")  # Telegram ещё есть
        self.press(f"nc:{c.pk}:tg:f")
        c.refresh_from_db()
        self.assertEqual((c.telegram, c.status), ("", "free"))  # писать некуда — отпустили, бот не предложит
        self.assertIn("blesk_channel", c.note)
        self.assertIsNone(self.tg_pick())

    def tg_pick(self):
        from .bot import pick_new
        return pick_new(self.denis)

    def test_prices(self):
        from .bot import prices_text
        self.client.force_login(self.denis)
        page = self.client.get(reverse("prices")).content.decode()
        self.assertIn("ИИ-ассистент", page)
        self.assertIn("7 000", page)
        self.assertIn("половина перед публикацией", page)
        text = prices_text().replace("\u202f", " ").replace("\xa0", " ")
        self.assertIn("ИИ-ассистент</b> — от 40 000 ₽ + 7 000 ₽ в месяц", text)
        self.assertIn("SEO-продвижение в Яндексе и Google</b> — от 40 000 ₽ в месяц", text)
        self.assertIn("Аудит с планом исправлений</b> — 5 000 ₽", text)


class BotTests(BotHelpers):
    def test_outreach_queue_sent_and_next(self):
        a = self.company("Блеск", "+7 900 111-22-33", score=4)
        b = self.company("Глянец", "+7 900 111-22-44", score=2)
        self.say("Новые")
        card = self.last()
        self.assertIn("Блеск", card["text"])
        self.assertIn("сегодня 0 из 50", card["text"])
        urls = [x.get("url", "") for x in self.buttons(card)]
        self.assertTrue(any(u.startswith("https://wa.me/79001112233?text=") for u in urls))
        a.refresh_from_db()
        self.assertEqual((a.status, a.assigned_to), ("taken", self.denis))
        self.press(f"os:{a.pk}:wa:f")
        a.refresh_from_db()
        self.assertEqual(a.status, "sent")
        self.assertEqual(a.touches.filter(kind="first", channel="wa").count(), 1)
        self.assertIn("Глянец", self.last()["text"])
        self.assertIn("сегодня 1 из 50", self.last()["text"])
        self.press(f"ok:{b.pk}:f")  # пропустить — вернётся завтра, больше компаний нет
        self.assertIn("не осталось", self.last()["text"])

    def test_replied_creates_lead_with_ready_answer(self):
        c = self.company("Блеск", "89001112233")
        outreach.take(c, self.denis)
        outreach.mark_sent(c, self.denis, "wa", "first")
        self.press(f"ra:{c.pk}:price")
        c.refresh_from_db()
        lead = c.lead
        self.assertEqual((c.status, lead.doer, lead.next_step_on), ("replied", self.denis, date.today() + timedelta(days=1)))
        msg = self.last()
        self.assertIn("25 000", msg["text"])
        self.assertTrue(self.buttons(msg)[0]["url"].startswith("https://wa.me/79001112233"))
        self.assertTrue(self.ivan.tg_messages.filter(text__contains="Ответили на рассылку").exists())

    def test_search_by_phone_and_name(self):
        c = self.company("Студия Блеск", "+7 (900) 111-22-33")
        outreach.take(c, self.ivan)
        outreach.mark_sent(c, self.ivan, "wa", "first")
        self.say("8 900 111 22 33")
        self.assertIn("Студия Блеск", self.last()["text"])
        self.assertIn("пишет Иван", self.last()["text"])
        self.say("блеск")
        self.assertIn("Студия Блеск", self.last()["text"])
        self.say("Ромашка")
        self.assertIn("не работали", self.last()["text"])

    def test_parse_lead(self):
        from .bot import parse_lead
        f = parse_lead("заявка Кофейня Зерно, Казань, +7 900 000-00-00, лендинг, 25 000")
        self.assertEqual((f["client"], f["city"], f["price"], f["service"].name), ("Кофейня Зерно", "Казань", 25000, "Лендинг"))
        self.assertEqual(f["phone"], "+7 900 000-00-00")
        f = parse_lead("заявка Ромашка, сео 30к")
        self.assertEqual((f["client"], f["price"], f["service"].name), ("Ромашка", 30000, "SEO-продвижение в Яндексе и Google"))
        f = parse_lead("Заявка ромашка Казань лендинг за 25 000")  # так приходит голосовое: без запятых
        self.assertEqual((f["client"], f["city"], f["price"], f["service"].name), ("Ромашка", "Казань", 25000, "Лендинг"))
        f = parse_lead("заявка Работа мечты, нужен бот")  # «бот» внутри «Работа» — не услуга
        self.assertEqual((f["client"], f["service"].name), ("Работа мечты", "Telegram-бот для заявок"))

    def test_quick_lead_and_step_buttons(self):
        self.say("заявка Кофейня Зерно, +7 900 000-00-00, лендинг, 25 000")
        lead = Lead.objects.get(client="Кофейня Зерно")
        self.assertEqual((lead.doer, lead.finder, lead.price, lead.source.name), (self.denis, self.denis, 25000, "Сам нашёл"))
        self.press(f"lw:{lead.number}:1")
        lead.refresh_from_db()
        self.assertEqual(lead.next_step_on, date.today() + timedelta(days=1))
        lead.next_step_on = date.today() - timedelta(days=2)
        lead.save()
        text, markup = self.tg.digest_message(self.denis)
        self.assertIn("Просрочено", text)
        datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertIn(f"ld:{lead.number}", datas)
        self.press(f"lp:{lead.number}", markup=markup)
        lead.refresh_from_db()
        self.assertEqual(lead.next_step_on, date.today() + timedelta(days=1))
        self.press(f"ld:{lead.number}", markup=markup)
        prompt = self.last()["text"]
        self.assertTrue(self.last()["reply_markup"]["force_reply"])
        self.say("созвон по брифу", reply_to=prompt)
        lead.refresh_from_db()
        self.assertEqual((lead.next_step, lead.next_step_on), ("созвон по брифу", None))
        self.press(f"lw:{lead.number}:3")
        lead.refresh_from_db()
        self.assertEqual(lead.next_step_on, date.today() + timedelta(days=3))

    def test_silent_client_nudge(self):
        lead = Lead.objects.create(client="Такт", source=Source.objects.get(name="Сам нашёл"), doer=self.denis,
                                   created_by=self.denis, stage="brief", phone="+79001112233", next_step="ждём", next_step_on=date.today() + timedelta(days=5),
                                   stage_changed_at=timezone.now() - timedelta(days=4))
        text, markup = self.tg.digest_message(self.denis)
        self.assertIn("Клиент молчит", text)
        self.press(f"ln:{lead.number}", markup=markup)
        self.assertIn("вопросы по сайту", self.last()["text"])
        self.assertEqual(self.tg.silent(Lead.objects.all()), [])

    def test_review_back_with_comment(self):
        lead = Lead.objects.create(client="Такт", source=Source.objects.get(name="Сам нашёл"), doer=self.denis,
                                   created_by=self.denis, stage="review")
        self.press(f"rv:{lead.number}:back", chat=111)
        lead.refresh_from_db()
        self.assertEqual(lead.stage, "work")
        self.say("поменяй фото на первом экране", chat=111, reply_to=self.last()["text"])
        self.assertTrue(self.denis.tg_messages.filter(text__contains="поменяй фото").exists())
        self.press(f"rv:{lead.number}:ok", chat=555)  # исполнитель сам себя не принимает
        self.assertFalse(lead.events.filter(text="Проект принят").exists())

    def test_evening_team_and_week_reports(self):
        from datetime import datetime
        from .models import StudioSettings
        c = self.company("Блеск", "89001112233")
        outreach.take(c, self.denis)
        outreach.mark_sent(c, self.denis, "wa", "first")
        now = timezone.make_aware(datetime.combine(date.today(), datetime.min.time()).replace(hour=19, minute=30))
        self.assertEqual(self.tg.queue_evening(now), 3)  # норма двоим + сверка ответов Денису
        self.assertTrue(self.denis.tg_messages.filter(text__contains="1 из 50").exists())
        self.assertTrue(self.denis.tg_messages.filter(text__contains="Кто ответил?").exists())
        self.assertEqual(self.tg.queue_evening(now), 0)
        self.say("/chat", chat=-100500)  # в группе без from владельца — не подключит
        self.tg.handle_update({"message": {"chat": {"id": -100500, "type": "supergroup"}, "from": {"id": 111}, "text": "/chat@nodus_crm_bot"}})
        self.assertEqual(StudioSettings.get().tg_team_chat, -100500)
        report = self.tg.day_report()
        self.assertIn("Денис — новых 1 из 50", report)
        self.assertTrue(self.tg.send_team_report(now.replace(hour=20, minute=5)))
        self.assertEqual(self.last()["chat_id"], -100500)
        self.assertIn("Итог недели", self.tg.week_report())
        self.assertIn("Заказов за ноябрь", self.tg.day_report(date(2026, 11, 5)))

    def test_team_chat_moves_to_supergroup(self):
        from datetime import datetime
        from .models import StudioSettings
        st = StudioSettings.get()
        st.tg_team_chat = -500
        st.save()
        # Telegram пишет в старую группу служебное сообщение о переезде
        self.tg.handle_update({"message": {"chat": {"id": -500, "type": "group"}, "migrate_to_chat_id": -100777}})
        self.assertEqual(StudioSettings.get().tg_team_chat, -100777)
        # служебное сообщение пропустили — итог дня сам узнаёт новый номер из отказа Telegram
        st = StudioSettings.get()
        st.tg_team_chat = -500
        st.save()

        def transport(token, method, params, timeout):
            self.sent.append((method, params))
            if params.get("chat_id") == -500:
                return {"ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                        "parameters": {"migrate_to_chat_id": -100777}}
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        self.tg.transport = transport
        now = timezone.make_aware(datetime.combine(date.today(), datetime.min.time()).replace(hour=20, minute=5))
        self.assertTrue(self.tg.send_team_report(now))
        self.assertEqual(self.last()["chat_id"], -100777)
        self.assertEqual(StudioSettings.get().tg_team_chat, -100777)

    def test_chat_watch(self):
        from . import chatwatch
        from .models import ChatHit
        self.assertEqual(chatwatch.match("Всем привет! Нужен сайт для кофейни, посоветуйте разработчика"), "сайт")
        self.assertIsNone(chatwatch.match("Делаю сайты недорого, нужен сайт — пишите"))
        self.assertIsNone(chatwatch.match("Ищу бариста в кофейню"))
        self.assertEqual(chatwatch.match("Кто может сделать SEO для салона?"), "seo")
        hit = chatwatch.record(-1001234, "Бизнес Ростов", None, 42, 9, "Анна", "anna_biz", "Нужен сайт, посоветуйте", "сайт")
        self.assertEqual(hit.link, "https://t.me/c/1234/42")
        msg = self.ivan.tg_messages.last()
        self.assertIn("Бизнес Ростов", msg.text)
        self.assertIn("https://t.me/anna_biz", json.dumps(msg.markup))
        chatwatch.record(-1009999, "Другой чат", "other", 5, 9, "Анна", "anna_biz", "Нужен сайт", "сайт")
        self.assertEqual(self.ivan.tg_messages.count(), 1)  # тот же автор за сутки — один раз
        self.press(f"ch:{hit.pk}", chat=111)
        hit.refresh_from_db()
        self.assertEqual((hit.status, hit.lead.telegram, hit.lead.source.name), ("lead", "@anna_biz", "Бизнес-чаты"))
        self.assertEqual(ChatHit.objects.count(), 2)

    def test_voice_to_lead(self):
        from . import voice
        old = voice.transcribe_file
        voice.transcribe_file = lambda file_id: "Заявка Ромашка, Казань, лендинг"
        try:
            self.tg.handle_update({"message": {"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "voice": {"file_id": "x"}}})
        finally:
            voice.transcribe_file = old
        self.assertTrue(Lead.objects.filter(client="Ромашка", city="Казань").exists())


class TextsTests(TestCase):
    """Тексты рассылки 05.11: выбор по зацепке, фраза по нише, учёт ответов по каждому тексту."""

    def setUp(self):
        self.denis = mk("Денис")

    def test_text_by_hook_and_niche(self):
        rest = Company.objects.create(name="Рыба", city="Казань", niche="Рестораны", phone="+79001112233")
        self.assertEqual(rest.hook, "no_site")  # без сайта зацепка ставится сама
        text, _ = outreach.message_for(rest, self.denis, "first")
        self.assertIn("Меня зовут Денис, я из студии Нодус", text)
        self.assertIn("можем сделать с меню и фото", text)
        self.assertNotIn("{", text)
        seo = Company.objects.create(name="Блеск", city="Казань", niche="Детейлинг", site="blesk.ru", phone="+79001112244",
                                     hook="seo", hook_text="детейлинг Казань")
        text, _ = outreach.message_for(seo, self.denis, "first")
        self.assertIn("По запросу «детейлинг Казань» вас нет", text)
        self.assertIn("сайты, ботов", text)  # в строке «если нужно другое» сайты есть
        self.assertIn("Прислать разбор", outreach.message_for(seo, self.denis, "followup")[0])
        crm = Company.objects.create(name="Опт", site="opt.ru", email="info@opt.ru", hook="crm")
        text, subject = outreach.message_for(crm, self.denis, "first", "email")
        self.assertEqual(subject, "Заявки в одном месте")
        self.assertIn("Битрикс24", text)

    def test_site_without_hook_is_not_offered(self):
        from .bot import ready
        self.assertFalse(ready(Company.objects.create(name="Есть сайт", site="x.ru", phone="+79001112233")))
        self.assertTrue(ready(Company.objects.create(name="Нет сайта", phone="+79001112244")))

    def test_touch_keeps_text_and_stats(self):
        c = Company.objects.create(name="Рыба", niche="Бары", phone="+79001112233")
        outreach.take(c, self.denis)
        outreach.mark_sent(c, self.denis, "wa", "first")
        t = c.touches.get(kind="first")
        self.assertEqual(t.template.hook, "no_site")
        outreach.convert_to_lead(c, self.denis)
        row = outreach.text_stats()[0]
        self.assertEqual((row["sent"], row["replied"], row["pct"]), (1, 1, 100.0))

    def test_import_hook_by_name(self):
        self.assertEqual(outreach.hook_code("Нет в Яндексе"), "seo")
        self.assertEqual(outreach.hook_code("calls"), "calls")
        self.assertEqual(outreach.hook_code("что-то"), "")


class BaseLoadTests(TestCase):
    """Загрузка базы из файла и ежедневный автосбор: без повторов, зацепки, итог владельцу."""

    def setUp(self):
        from . import sitecheck
        self.ivan = mk("Иван", owner=True)
        Profile.objects.filter(user=self.ivan).update(tg_chat_id=1)
        from .models import StudioSettings
        st = StudioSettings.get()
        st.tg_token = "1:x"
        st.save()
        self.old_check = sitecheck.check
        sitecheck.check = lambda url, timeout=12: ("down", "домена нет") if "dead" in url else ("up", "")

    def tearDown(self):
        from . import sitecheck
        sitecheck.check = self.old_check

    def test_import_base(self):
        import tempfile
        from django.core.management import call_command
        Company.objects.create(name="Старая", city="Казань", phone="+79000000001")
        rows = [{"name": "Старая", "city": "Казань", "phone": "+79000000001"},
                {"name": "Без сайта", "city": "Казань", "niche": "Бары", "phone": "+79000000002"},
                {"name": "Мёртвый сайт", "city": "Казань", "phone": "+79000000003", "site": "dead.ru"},
                {"name": "Живой сайт", "city": "Казань", "phone": "+79000000004", "site": "alive.ru"},
                {"name": "SEO", "city": "Казань", "phone": "+79000000005", "site": "alive2.ru", "зацепка": "Нет в Яндексе", "факт": "бар Казань"}]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        call_command("import_base", f.name, batch="Тест", stdout=__import__("io").StringIO())
        got = {c.name: (c.hook, c.hook_text) for c in Company.objects.filter(batch="Тест")}
        self.assertEqual(got, {"Без сайта": ("no_site", ""), "Мёртвый сайт": ("site_down", "dead.ru"),
                               "Живой сайт": ("site_ok", ""), "SEO": ("seo", "бар Казань")})
        self.assertIn("База пополнена: +4", self.ivan.tg_messages.last().text)

    def test_import_experts_twice(self):
        import tempfile
        from django.core.management import call_command
        rows = [{"name": "Психолог | онлайн", "niche": "Психолог", "instagram": "anna_psy", "hook": "expert",
                 "external_id": "ig-anna_psy", "map_url": "https://instagram.com/anna_psy"}]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        for _ in range(2):  # второй сбор того же эксперта — повтор, по коду карточки
            call_command("import_base", f.name, batch="Эксперты", stdout=__import__("io").StringIO())
        c = Company.objects.get(batch="Эксперты")
        self.assertEqual((c.external_id, c.instagram, c.hook), ("ig-anna_psy", "anna_psy", "expert"))


class RolesTests(TestCase):
    """Защита базы и панель «Команда»: контакты видит взявший и владелец, брать по одной без конца нельзя."""

    def setUp(self):
        self.ivan = mk("Иван", owner=True)
        self.denis = mk("Денис")
        self.c = Company.objects.create(name="Блеск", phone="+79001112233", telegram="@blesk")

    def test_contacts_hidden_until_taken(self):
        self.client.force_login(self.denis)
        page = self.client.get(reverse("company", args=[self.c.pk])).content.decode()
        self.assertNotIn("79001112233", page)
        self.assertNotIn("@blesk", page)
        self.assertEqual(self.client.get(reverse("company_edit", args=[self.c.pk])).status_code, 302)
        self.client.post(reverse("company_action", args=[self.c.pk, "take"]))
        page = self.client.get(reverse("company", args=[self.c.pk])).content.decode()
        self.assertIn("@blesk", page)
        self.client.force_login(self.ivan)
        self.assertIn("@blesk", self.client.get(reverse("company", args=[self.c.pk])).content.decode())

    def test_take_limit(self):
        for i in range(outreach.TAKE_LIMIT):
            outreach.take(Company.objects.create(name=f"К {i}", phone=f"+7911000{i:04d}"), self.denis)
        with self.assertRaises(outreach.TakeLimit):
            outreach.take(self.c, self.denis)
        outreach.take(self.c, self.ivan)  # владельцу можно

    def test_team_panel_owner_only(self):
        self.client.force_login(self.denis)
        self.assertEqual(self.client.get(reverse("team")).status_code, 403)
        outreach.take(self.c, self.denis)
        outreach.mark_sent(self.c, self.denis, "wa", "first")
        self.client.force_login(self.ivan)
        page = self.client.get(reverse("team")).content.decode()
        self.assertIn("Денис", page)
        self.assertEqual(self.client.get(reverse("outreach") + f"?tab=all&who={self.denis.pk}").status_code, 200)


class TodayTests(TestCase):
    """«Сегодня»: норма дня считает отправленное из CRM, ответ отмечается в один клик и сразу даёт заявку."""

    def setUp(self):
        self.ivan = mk("Иван", owner=True)
        self.denis = mk("Денис")
        self.c = Company.objects.create(name="Блеск", city="Ростов-на-Дону", phone="+79001112233")
        outreach.take(self.c, self.denis)
        outreach.mark_sent(self.c, self.denis, "wa", "first")
        self.client.force_login(self.denis)

    def test_day_counts_and_waiting(self):
        page = self.client.get(reverse("home")).content.decode()
        self.assertIn("1<small> из 50", page)
        self.assertIn("1<small> / 25", page)  # WhatsApp
        self.assertIn("Блеск", page)  # ждёт отметки ответа
        self.assertIn("отметить ответы у 1 компании", page)

    def test_all_waiting_shown(self):
        for i in range(20):
            c = Company.objects.create(name=f"Студия {i}", phone=f"+7911000{i:04d}")
            outreach.take(c, self.denis)
            outreach.mark_sent(c, self.denis, "tg", "first")
        page = self.client.get(reverse("home")).content.decode()
        self.assertIn("Студия 0<", page)
        self.assertIn("Студия 19<", page)
        self.assertIn("отметить ответы у 21 компании", page)

    def test_find_by_phone(self):
        page = self.client.get(reverse("home") + "?q=8 900 111-22-33").content.decode()
        self.assertIn("Блеск", page)
        self.assertIn('value="price"', page)

    def test_replied_with_kind(self):
        r = self.client.post(reverse("company_action", args=[self.c.pk, "replied"]), {"kind": "price"})
        lead = Lead.objects.get()
        self.assertRedirects(r, reverse("lead", args=[lead.number]), fetch_redirect_response=False)
        self.assertEqual(lead.next_step, outreach.REPLY_STEPS["price"][0])
        self.assertEqual(lead.finder, self.denis)
        self.c.refresh_from_db()
        self.assertEqual(self.c.status, "replied")

    def test_replied_no_is_refusal(self):
        self.client.post(reverse("company_action", args=[self.c.pk, "replied"]), {"kind": "no", "next": reverse("home")})
        self.c.refresh_from_db()
        self.assertEqual(self.c.status, "refused")
        self.assertFalse(Lead.objects.exists())

    def test_sent_without_followup_date_gets_one(self):
        Company.objects.filter(pk=self.c.pk).update(next_touch_on=None, hold_until=date.today())
        outreach.refresh_statuses()
        self.c.refresh_from_db()
        self.assertEqual(self.c.status, "sent")
        self.assertEqual(self.c.next_touch_on, date.today())
        self.assertGreater(self.c.hold_until, date.today())

    def test_only_writer_marks_reply(self):
        other = mk("Костя")
        self.client.force_login(other)
        self.client.post(reverse("company_action", args=[self.c.pk, "replied"]), {"kind": "yes"})
        self.assertFalse(Lead.objects.exists())
        self.client.force_login(self.ivan)
        self.client.post(reverse("company_action", args=[self.c.pk, "replied"]), {"kind": "yes"})
        self.assertEqual(Lead.objects.get().finder, self.denis)


class HelpTests(TestCase):
    def test_help_numbers_from_settings_and_owner_part(self):
        ivan, denis = mk("Иван", owner=True), mk("Денис")
        self.client.force_login(denis)
        page = self.client.get(reverse("help")).content.decode()
        self.assertIn("Норма — 50 новых в день", page)
        self.assertIn("Узнать бюджет и предложить вариант, завтра", page)
        self.assertNotIn("Только для Ивана", page)
        self.client.force_login(ivan)
        self.assertIn("Только для Ивана", self.client.get(reverse("help")).content.decode())

    def test_harvest_page_is_about_yandex(self):
        self.client.force_login(mk("Иван", owner=True))
        page = self.client.get(reverse("harvest")).content.decode()
        self.assertIn("Следующий сбор", page)
        self.assertNotIn("2ГИС", page)


class SborTests(TestCase):
    """Сбор с Яндекса на сервере: план, отбор собранного, запоминание пар и регионов, замеры скорости."""

    def setUp(self):
        self.ivan = mk("Иван", owner=True)

    def test_plan_and_build(self):
        from .models import City, StudioSettings
        from .sbor import build, plan
        p = plan(5)
        self.assertEqual(len(p["plan"]), 5)
        job = p["plan"][0]
        items = {
            "1": {"id": "1", "t": "Блеск", "c": ["Детейлинг"], "r": 4.9, "n": 120, "u": [], "s": [["telegram", "t.me/blesk_kzn"]],
                  "p": ["+7 900 111-22-33"], "a": "ул. Ленина, 1", "st": "open", "ad": True, "pr": 30000,
                  "city": job["city"], "niche": job["niche"], "hook": "", "q": job["query"]},
            "2": {"id": "2", "t": "Мало", "c": ["Детейлинг"], "r": 4.9, "n": 5, "s": [], "p": ["+7 900 111-22-44"], "st": "open"},
            "3": {"id": "3", "t": "Закрыто", "c": ["Бар"], "r": 5, "n": 300, "s": [], "p": ["+7 900 111-22-55"], "st": "permanent-closed"},
            "4": {"id": "4", "t": "Стационар", "c": ["Бар"], "r": 5, "n": 300, "s": [], "p": ["+7 843 111-22-55"], "st": "open"},
            "5": {"id": "5", "t": "Глянец", "c": ["Детейлинг"], "r": 4.9, "n": 80, "s": [["whatsapp", "https://wa.me/79001112266"]],
                  "p": ["+7 843 111-22-66"], "st": "open"},
        }
        cards, stats = build({"items": items, "regions": {job["city"]: "43/kazan"}, "jobs": [f"{job['city']}|{job['niche']}"]})
        self.assertEqual([c["name"] for c in cards], ["Блеск", "Глянец"])
        self.assertEqual((cards[0]["telegram"], cards[0]["whatsapp"], cards[0]["wa_source"], cards[0]["ads"]),
                         ("@blesk_kzn", "79001112233", "mobile", True))
        self.assertEqual((cards[1]["whatsapp"], cards[1]["wa_source"]), ("79001112266", "card"))
        # «Мало»: только мобильный без ссылки WhatsApp — писать некуда (решение 07.11), до отзывов не доходит
        self.assertEqual(stats["отсев"], {"закрыта": 1, "нет WhatsApp и Telegram": 2})
        self.assertEqual(City.objects.get(name=job["city"]).ya_region, "43/kazan")
        self.assertIn(f"{job['city']}|{job['niche']}", StudioSettings.get().ya_done)
        self.assertNotEqual(plan(5)["plan"][0], job)  # собранная пара уходит в конец очереди

    def test_contacts_apply(self):
        import tempfile
        from io import StringIO
        from django.core.management import call_command
        from .bot import channels
        a = Company.objects.create(name="Канал", telegram="@kanal_shop", phone="+79000000001", wa_source="mobile")
        b = Company.objects.create(name="Личный", telegram="@lichny", phone="+79000000002")
        c = Company.objects.create(name="Карточка", phone="+79000000003", map_url="https://yandex.ru/maps/org/5/")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"pk": a.pk, "tg": "channel"}, {"pk": b.pk, "tg": "personal"}, {"pk": c.pk, "wa_source": "card"}], f)
        call_command("contacts_apply", f.name, stdout=StringIO())
        a.refresh_from_db(), b.refresh_from_db(), c.refresh_from_db()
        self.assertEqual((a.telegram, channels(a)), ("", []))  # канал убран, угаданный WhatsApp не считается
        self.assertIn("kanal_shop — канал", a.note)
        self.assertEqual(channels(b), ["wa", "tg"])
        self.assertEqual((c.wa_source, channels(c)), ("card", ["wa"]))

    def test_speed_apply(self):
        import tempfile
        from io import StringIO
        from django.core.management import call_command
        a = Company.objects.create(name="Медленный", site="slow.ru", hook="site_ok", phone="+79000000001")
        b = Company.objects.create(name="Быстрый", site="fast.ru", hook="site_ok", phone="+79000000002")
        c = Company.objects.create(name="Сбой", site="err.ru", hook="site_ok", phone="+79000000003")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"pk": a.pk, "lcp": 9.7}, {"pk": b.pk, "lcp": 2.1}, {"pk": c.pk, "lcp": None}], f)
        call_command("speed_apply", f.name, stdout=StringIO())
        a.refresh_from_db(), b.refresh_from_db(), c.refresh_from_db()
        self.assertEqual((a.hook, a.hook_text), ("site_error", "по замеру Google PageSpeed на телефоне главная загружается дольше 9 секунд"))
        self.assertEqual((b.hook, str(b.lcp), str(c.lcp)), ("site_ok", "2.1", "-1.0"))


class LoginTests(TestCase):
    def test_login_ignores_case_and_spaces(self):
        Profile.objects.create(user=User.objects.create_user("denis", password="test-pass-123", first_name="Денис"))
        r = self.client.post(reverse("login"), {"username": " Denis ", "password": "test-pass-123"})
        self.assertEqual(r.status_code, 302)
        self.client.logout()
        r = self.client.post(reverse("login"), {"username": "denis", "password": "неверный"})
        self.assertEqual(r.status_code, 200)


class LeadFlowTests(BotHelpers):
    """Цепочка 05.11: вечерняя сверка ответов, «Мои заявки» в боте, правки после полной оплаты и закрытие."""

    def test_evening_reply_check(self):
        from .telegram import reply_check
        c = self.company("Блеск", "+79001112233")
        outreach.take(c, self.denis)
        outreach.mark_sent(c, self.denis, "wa", "first")
        text, markup = reply_check(self.denis)
        self.assertIn("Блеск", text)
        self.press(f"ev:{c.pk}", markup=markup)
        self.assertIn("Что ответили?", self.last()["text"])
        self.press(f"ra:{c.pk}:yes")
        c.refresh_from_db()
        self.assertEqual(c.status, "replied")
        d = self.company("Глянец", "+79001112244")
        outreach.take(d, self.denis)
        outreach.mark_sent(d, self.denis, "wa", "first")
        self.press(f"ex:{d.pk}")
        d.refresh_from_db()
        self.assertEqual(d.status, "refused")

    def test_my_leads_stage_step_lost(self):
        lead = Lead.objects.create(client="Такт", source=Source.objects.get(name="Сам нашёл"), doer=self.denis,
                                   created_by=self.denis, stage="contacted")
        self.say("Заявки")
        self.assertIn("Такт", self.last()["text"])
        self.press(f"lv:{lead.number}")
        self.assertTrue(any(b["callback_data"] == f"ls:{lead.number}:brief" for b in self.buttons(self.last())))
        self.press(f"ls:{lead.number}:brief")
        lead.refresh_from_db()
        self.assertEqual(lead.stage, "brief")
        self.press(f"ls:{lead.number}:review")
        self.assertTrue(self.ivan.tg_messages.filter(text__contains="Проверь проект").exists())
        self.press(f"lx:{lead.number}")
        self.say("дорого", reply_to=self.last()["text"])
        lead.refresh_from_db()
        self.assertEqual((lead.stage, lead.lost_reason), ("lost", "дорого"))

    def test_full_payment_starts_fixes_then_close(self):
        from .models import PayerType
        lead = Lead.objects.create(client="Такт", source=Source.objects.get(name="Сам нашёл"), doer=self.denis, receiver=self.denis,
                                   payer=PayerType.objects.first(), created_by=self.denis, stage="delivered", price=30000)
        Payment.objects.create(lead=lead, amount=30000)
        self.assertTrue(lead.maybe_start_fixes())
        lead.refresh_from_db()
        self.assertEqual((lead.stage, lead.next_step_on), ("fixes", date.today() + timedelta(days=14)))
        Lead.objects.filter(pk=lead.pk).update(stage_changed_at=timezone.now() - timedelta(days=15))
        text, markup = self.tg.digest_message(self.denis)
        self.assertIn("закрываем?", text)
        self.press(f"lc:{lead.number}", markup=markup)
        lead.refresh_from_db()
        self.assertEqual(lead.stage, "closed")


class AgentTests(BotHelpers):
    """Агент рассылки (10.11): кому пишет, лимиты, мёртвые контакты, остановка канала, ответы, управление."""

    def setUp(self):
        super().setUp()
        from .models import StudioSettings
        st = StudioSettings.get()
        st.agent_on, st.agent_user, st.agent_wa_cap, st.agent_new_cap = True, self.ivan, 1, 60
        st.save()

    def at(self, h, m=0):
        from datetime import datetime
        from unittest import mock
        from zoneinfo import ZoneInfo
        return mock.patch("django.utils.timezone.now", return_value=datetime(2026, 11, 10, h, m, tzinfo=ZoneInfo("Europe/Moscow")))

    def test_telegram_first_and_whatsapp_cap(self):
        from . import agent
        a = self.company("Блеск", "+7 900 111-22-33", score=5)
        b = self.company("Глянец", "+7 900 111-22-44", score=4, telegram="@glyanec")
        self.company("Кристалл", "+7 900 111-22-55", score=3)
        with self.at(12):
            job = agent.plan({"wa", "tg"})
            self.assertEqual((job.company_id, job.channel, job.to, job.kind), (a.pk, "wa", "79001112233", "first"))
            self.assertEqual(agent.record(job, {"ok": True}), "sent")
            a.refresh_from_db()
            self.assertEqual((a.status, a.assigned_to), ("sent", self.ivan))
            self.assertTrue(a.touches.get(kind="first").auto)
            job = agent.plan({"wa", "tg"})  # у «Глянца» есть Telegram — туда, WhatsApp бережём
            self.assertEqual((job.company_id, job.channel, job.to), (b.pk, "tg", "glyanec"))
            agent.record(job, {"ok": True})
            self.assertIsNone(agent.plan({"wa", "tg"}))  # WhatsApp на сегодня исчерпан (1), у «Кристалла» только он
            self.assertIn("Агент рассылки: работает", agent.status_text(wa={"status": "open", "me": "79280000000"}))

    def test_free_company_taken_at_plan_and_pace(self):
        from . import agent
        from .models import StudioSettings
        a = self.company("Блеск", "+7 900 111-22-33")
        with self.at(12):
            job = agent.plan({"wa"})
        a.refresh_from_db()
        self.assertEqual((a.status, a.assigned_to), ("taken", self.ivan))  # пока уходит сообщение, бот её никому не даст
        st = StudioSettings.get()
        gaps = [agent.gap(st, "wa") for _ in range(50)]
        self.assertTrue(all(30 <= g <= 48 for g in gaps))   # не чаще 2 в минуту и не ровным шагом
        self.assertGreater(len({round(g) for g in gaps}), 5)
        self.assertTrue(all(12 <= agent.gap(st, "tg") <= 19.2 for _ in range(50)))  # Telegram — до 5 в минуту
        self.client.force_login(self.ivan)
        self.client.post(reverse("telegram"), {"op": "agent", "agent_on": "1", "agent_wa_gap": "3", "agent_tg_gap": "1",
                                               "agent_new_cap": "60", "agent_wa_cap": "20", "agent_spread": "1"})
        st = StudioSettings.get()
        self.assertEqual((st.agent_wa_gap, st.agent_tg_gap, st.agent_spread), (20, 5, True))
        self.assertEqual(job.company_id, a.pk)

    def test_outside_hours_and_new_cap(self):
        from . import agent
        from .models import StudioSettings
        self.company("Блеск", "+7 900 111-22-33")
        with self.at(21):
            self.assertIsNone(agent.plan({"wa"}))
            self.assertLessEqual(agent.pause(), 3600)
        st = StudioSettings.get()
        st.agent_new_cap = 0
        st.save()
        with self.at(12):
            self.assertIsNone(agent.plan({"wa"}))
        st.agent_on = False
        st.save()
        with self.at(12):
            self.assertIsNone(agent.plan({"wa"}))

    def test_dead_contact_dropped_and_timelock_stops_whatsapp(self):
        from . import agent
        from .models import StudioSettings
        a = self.company("Блеск", "+7 900 111-22-33")
        with self.at(12):
            job = agent.plan({"wa"})
            self.assertEqual(agent.record(job, {"ok": False, "reason": "not_on_whatsapp"}), "dropped")
            a.refresh_from_db()
            self.assertEqual(a.wa_source, "none")
            b = self.company("Глянец", "+7 900 111-22-44")
            job = agent.plan({"wa"}, skip={a.pk})
            self.assertEqual(job.company_id, b.pk)
            self.assertEqual(agent.record(job, {"ok": False, "reason": "timelock"}), "stopped")
            self.assertIsNone(agent.plan({"wa"}))
            self.assertTrue(self.ivan.tg_messages.filter(text__contains="ограничил").exists())
            st = StudioSettings.get()
            self.assertTrue(agent.stopped(st.agent_state, "wa"))

    def test_three_errors_stop_channel(self):
        from . import agent
        self.company("Блеск", "+7 900 111-22-33")
        with self.at(12):
            for _ in range(3):
                job = agent.plan({"wa"})
                self.assertEqual(agent.record(job, {"ok": False, "reason": "error", "error": "boom"}), "error")
            self.assertIsNone(agent.plan({"wa"}))
            self.assertTrue(self.ivan.tg_messages.filter(text__contains="3 ошибки подряд").exists())

    def test_followup_goes_to_same_channel(self):
        from . import agent
        from .models import Touch
        c = self.company("Блеск", "+7 900 111-22-33", telegram="@blesk")
        with self.at(12):
            outreach.mark_sent(c, self.ivan, "wa", "first")
            c.refresh_from_db()
            c.next_touch_on = date(2026, 11, 10)
            c.save()
            job = agent.plan({"wa", "tg"})
            self.assertEqual((job.company_id, job.channel, job.kind), (c.pk, "wa", "followup"))
            agent.record(job, {"ok": True})
            c.refresh_from_db()
            self.assertEqual(c.status, "followup")
            self.assertTrue(Touch.objects.get(company=c, kind="followup").auto)

    def test_reply_in_whatsapp_and_telegram_notifies_once(self):
        from . import agent
        a = self.company("Блеск", "+7 900 111-22-33")
        b = self.company("Глянец", "+7 900 111-22-44", telegram="https://t.me/Glyanec")
        for c, ch in ((a, "wa"), (b, "tg")):
            outreach.mark_sent(c, self.ivan, ch, "first")
        self.assertEqual(agent.incoming("wa", "79001112233", "Да, интересно"), a)
        self.assertIsNone(agent.incoming("wa", "79001112233", "Ещё сообщение"))
        self.assertEqual(agent.incoming("tg", ["glyanec"], "Сколько стоит?"), b)
        self.assertIsNone(agent.incoming("wa", "79990000000", "Чужой"))
        msgs = list(self.ivan.tg_messages.filter(text__contains="Ответили в"))
        self.assertEqual(len(msgs), 2)
        self.assertIn("ev:", json.dumps(msgs[0].markup))

    def test_bot_command_pause_and_settings_page(self):
        from .models import StudioSettings
        self.say("агент", chat=111)
        self.assertIn("Агент рассылки: работает", self.last()["text"])
        self.press("ag:off", chat=111)
        self.assertFalse(StudioSettings.get().agent_on)
        self.press("ag:on", chat=555)  # Денис не владелец
        self.assertFalse(StudioSettings.get().agent_on)
        self.client.force_login(self.ivan)
        r = self.client.post(reverse("telegram"), {"op": "agent", "agent_on": "1", "agent_user": self.ivan.pk,
                                                   "agent_new_cap": "60", "agent_wa_cap": "40", "agent_followups": "1"})
        self.assertEqual(r.status_code, 302)
        st = StudioSettings.get()
        self.assertEqual((st.agent_on, st.agent_new_cap, st.agent_wa_cap), (True, 60, st.wa_daily_cap))
        page = self.client.get(reverse("telegram"))
        self.assertContains(page, "Агент рассылки")
        self.assertContains(page, "Шлюз WhatsApp на сервере не запущен")
        self.client.force_login(self.denis)
        self.client.post(reverse("telegram"), {"op": "agent"})
        self.assertTrue(StudioSettings.get().agent_on)

    def test_runner_cycle_sends_waits_and_reads_replies(self):
        from asgiref.sync import async_to_sync
        from crm.management.commands.agent import Runner

        class Gate:
            def __init__(self):
                self.sent, self.box = [], []

            def status(self, quota=False):
                return {"ok": True, "status": "open", "me": "79280000000"}

            def send(self, phone, text, first):
                self.sent.append((phone, first))
                return {"ok": True}

            def inbox(self, after):
                return {"ok": True, "items": [x for x in self.box if x["n"] > after], "last": len(self.box)}

        a = self.company("Блеск", "+7 900 111-22-33", score=5)
        self.company("Глянец", "+7 900 111-22-44", score=4)
        from .models import StudioSettings
        st = StudioSettings.get()
        st.agent_wa_cap = 20
        st.save()
        r = Runner(None)
        r.gate = Gate()
        with self.at(12):
            async_to_sync(r.cycle)()
            self.assertEqual(r.gate.sent, [("79001112233", True)])
            async_to_sync(r.cycle)()  # пауза WhatsApp ещё не прошла
            self.assertEqual(len(r.gate.sent), 1)
            r.next_at["wa"] = 0
            r.gate.box.append({"n": 1, "phone": "79001112233", "text": "Да, расскажите"})
            async_to_sync(r.cycle)()
            self.assertEqual([p for p, _ in r.gate.sent], ["79001112233", "79001112244"])
            self.assertTrue(self.ivan.tg_messages.filter(text__contains="Блеск").filter(text__contains="Ответили").exists())
            async_to_sync(r.cycle)()  # тот же ответ второй раз не приходит
            self.assertEqual(self.ivan.tg_messages.filter(text__contains="Ответили").count(), 1)
        a.refresh_from_db()
        self.assertEqual(a.status, "sent")

    def test_whatsapp_qr_page(self):
        from unittest import mock
        from . import agent
        self.client.force_login(self.ivan)
        with mock.patch.object(agent.WaGate, "status", return_value={"ok": True, "status": "qr", "qr": True, "me": ""}), \
                mock.patch.object(agent.WaGate, "qr_svg", return_value="<svg>qr</svg>"), \
                mock.patch.object(agent.WaGate, "link_qr", return_value={"ok": True}) as link_qr:
            self.client.post(reverse("telegram"), {"op": "wa_qr"})
            link_qr.assert_called_once()
            page = self.client.get(reverse("telegram"))
            self.assertContains(page, 'id="waqr"')
            r = self.client.get(reverse("wa_qr"))
            self.assertEqual((r.status_code, r["Content-Type"], r.content), (200, "image/svg+xml", b"<svg>qr</svg>"))
            self.assertEqual(self.client.get(reverse("wa_state")).json(), {"status": "qr", "qr": True})
        with mock.patch.object(agent.WaGate, "status", return_value={"ok": True, "status": "connecting"}):
            self.assertContains(self.client.get(reverse("telegram")), "Подключается")
        with mock.patch.object(agent.WaGate, "qr_svg", return_value=""):
            self.assertEqual(self.client.get(reverse("wa_qr")).status_code, 404)
        self.client.force_login(self.denis)
        self.assertEqual(self.client.get(reverse("wa_qr")).status_code, 403)

    def test_telegram_login_with_code_and_password(self):
        from unittest import mock
        from . import tguser
        from .models import StudioSettings
        st = StudioSettings.get()
        st.tg_api_id, st.tg_api_hash = "123", "abc"
        st.save()
        self.client.force_login(self.ivan)
        with mock.patch.object(tguser, "send_code", return_value={"phone": "+79001112233", "hash": "h", "s": "tmp"}) as send_code:
            self.client.post(reverse("telegram"), {"op": "tg_code", "phone": "8 (900) 111-22-33"})
        self.assertEqual(send_code.call_args[0][1], "+79001112233")
        self.assertContains(self.client.get(reverse("telegram")), "Код из Telegram")
        with mock.patch.object(tguser, "sign_in", return_value=(None, "password")):
            self.client.post(reverse("telegram"), {"op": "tg_signin", "code": "12 345"})
        self.assertTrue(StudioSettings.get().tg_login.get("need_password"))
        self.assertContains(self.client.get(reverse("telegram")), "Пароль двухэтапной проверки")
        with mock.patch.object(tguser, "sign_in", return_value=("SESSION", "Иван")) as sign_in:
            self.client.post(reverse("telegram"), {"op": "tg_signin", "code": "-", "password": "secret"})
        self.assertEqual(sign_in.call_args[0][3], "secret")
        st = StudioSettings.get()
        self.assertEqual((st.tg_session, st.tg_login, st.agent_state.get("tg_me")), ("SESSION", {}, "Иван"))
        self.assertContains(self.client.get(reverse("telegram")), "вошёл как Иван")
        with mock.patch.object(tguser, "log_out") as log_out:
            self.client.post(reverse("telegram"), {"op": "tg_logout"})
        log_out.assert_called_once()
        self.assertEqual(StudioSettings.get().tg_session, "")

    def test_bot_and_agent_do_not_write_to_the_same_company(self):
        from . import agent, bot
        wa_only = self.company("Блеск", "+7 900 111-22-33", score=9)
        both = self.company("Глянец", "+7 900 111-22-44", score=5, telegram="@glyanec")
        old = self.company("Старый", "+7 900 111-22-55", score=1)
        with self.at(12):
            outreach.mark_sent(old, self.ivan, "wa", "first")
            old.refresh_from_db()
            old.next_touch_on = date(2026, 11, 10)
            old.save()
            self.assertIsNone(bot.pick_followup(self.ivan))  # повтор в WhatsApp отправит агент
            c = bot.pick_new(self.ivan)  # Ивану руками — компания, где есть Telegram, а не та, что ждёт агента
            self.assertEqual(c.pk, both.pk)
            job = agent.plan({"wa"})  # агент не берёт компанию, которую Иван уже открыл в боте
            self.assertNotEqual(job.company_id, both.pk)
            self.assertIn(job.company_id, (wa_only.pk, old.pk))
        self.assertEqual(bot.pick_new(self.denis).pk, wa_only.pk)  # у Дениса агента нет — порядок прежний


class ChannelFilterTests(BotHelpers):
    """Фильтр «только Telegram / только WhatsApp» (Иван 10.11): общий для бота и CRM."""

    def test_bot_and_crm_filter(self):
        wa = self.company("Блеск", "+7 900 111-22-33", score=9)
        tg = self.company("Глянец", "", score=5, telegram="@glyanec")
        self.say("только телеграм")
        card = self.last()
        self.assertIn("Глянец", card["text"])
        self.assertIn("✓ Только Telegram", [b["text"] for b in self.buttons(card)])
        self.press(f"ok:{tg.pk}:f")  # пропустить — больше компаний с Telegram нет
        self.assertIn("С фильтром «только telegram» новых сейчас нет", self.last()["text"])
        self.press("qc:all:f")
        self.assertIn("Блеск", self.last()["text"])
        self.client.force_login(self.denis)
        page = self.client.get(reverse("outreach") + "?tab=mine&ch=tg")
        self.assertContains(page, "Глянец")
        self.assertNotContains(page, "Блеск")
        self.denis.profile.refresh_from_db()
        self.assertEqual(self.denis.profile.queue_channel, "tg")
        page = self.client.get(reverse("outreach") + "?tab=mine&ch=")
        self.assertContains(page, "Блеск")
        tg.refresh_from_db()
        wa.refresh_from_db()
        self.assertEqual((tg.assigned_to, wa.assigned_to), (self.denis, self.denis))

    def test_tg_found_behind_many_better_wa(self):
        """10.11: у Ивана «Только Telegram» давал «новых нет» — 500 лучших по баллу были без Telegram."""
        Company.objects.bulk_create([Company(name=f"Мойка {i}", city="Казань", niche="Детейлинг", phone=f"+7 863 {i:03d}-00-00",
                                             score=9, hook="no_site") for i in range(520)])
        self.company("Глянец", "", score=0, telegram="@glyanec", hook="no_site")
        self.say("только телеграм")
        self.assertIn("Глянец", self.last()["text"])
        self.client.force_login(self.denis)
        page = self.client.get(reverse("outreach") + "?tab=free&ch=tg")
        self.assertContains(page, 'Свободные <span class="n">0</span>')  # Глянец уже взят ботом, остальные без Telegram
