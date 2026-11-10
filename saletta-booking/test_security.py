"""Проверки защиты: кто что видит, перебор паролей, злоупотребления страницей гостя."""

import io
import json
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from PIL import Image

from booking import sms
from booking.models import Booking, BookingEvent, Guest, Membership, Message, Restaurant, Role, TableHold

from .base import TODAY, Base, User

TOMORROW = TODAY + timedelta(days=1)
KEY = 'k' * 24


def restrict(user, *restaurants):
    m = Membership.objects.get(user=user)
    m.all_restaurants = False
    m.save()
    m.restaurants.set(restaurants)
    return m


class RestaurantAccess(Base):
    """Управляющий второго ресторана не должен дотягиваться до первого: ни через сотрудников, ни через гостей."""

    def setUp(self):
        super().setUp()
        self.second = Restaurant.objects.create(org=self.org, name='Второй', slug='second', open_min=720, close_min=1380)
        self.boss = self.member('boss', Role.MANAGER)          # управляющий всей сети
        self.man = self.member('man', Role.MANAGER)
        restrict(self.man, self.second)
        self.mc = self.login(self.man)

    def test_staff_list_and_actions_stay_within_own_restaurants(self):
        rows = self.get('/api/staff', client=self.mc)
        self.assertEqual([x['login'] for x in rows['staff']], ['man'])
        self.assertEqual((rows['assignable'], rows['canAll']), (['admin', 'viewer'], False))
        boss_id = Membership.objects.get(user=self.boss).id
        owner_id = Membership.objects.get(user=self.owner).id
        self.post(f'/api/staff/{boss_id}', {'op': 'reset'}, client=self.mc, expect=404)
        self.post(f'/api/staff/{owner_id}', {'op': 'role', 'role': 'viewer'}, client=self.mc, expect=404)
        self.post('/api/staff', {'name': 'Вася', 'login': 'vasya', 'role': 'admin', 'rests': 'all'}, client=self.mc, expect=403)
        self.post('/api/staff', {'name': 'Вася', 'login': 'vasya', 'role': 'admin', 'rests': [self.r.id]}, client=self.mc, expect=404)
        self.post('/api/staff', {'name': 'Вася', 'login': 'vasya', 'role': 'manager'}, client=self.mc, expect=400)
        made = self.post('/api/staff', {'name': 'Вася', 'login': 'vasya', 'role': 'admin'}, client=self.mc)
        row = next(x for x in made['staff'] if x['login'] == 'vasya')
        self.assertEqual((row['access'], row['editable']), ([self.second.id], True))
        self.post(f'/api/staff/{row["id"]}', {'op': 'access', 'rests': 'all'}, client=self.mc, expect=403)
        # Новый сотрудник (уже сменивший временный пароль) видит только свой ресторан.
        User.objects.filter(username='vasya').update(must_change_password=False)
        vasya = self.login(User.objects.get(username='vasya'))
        self.assertEqual([r['name'] for r in self.get('/api/bootstrap', client=vasya)['restaurants']], ['Второй'])

    def test_manager_does_not_touch_other_managers(self):
        peer = self.member('peer', Role.MANAGER)
        restrict(peer, self.second)
        peer_row = next(x for x in self.get('/api/staff', client=self.mc)['staff'] if x['login'] == 'peer')
        self.assertFalse(peer_row['editable'])
        self.post(f'/api/staff/{peer_row["id"]}', {'op': 'reset'}, client=self.mc, expect=403)
        # Владелец может всё: и сбросить пароль управляющему, и открыть ему всю сеть.
        self.assertIn('password', self.post(f'/api/staff/{peer_row["id"]}', {'op': 'reset'})['issued'])
        self.post(f'/api/staff/{peer_row["id"]}', {'op': 'access', 'rests': 'all'})
        self.assertTrue(Membership.objects.get(user=peer).all_restaurants)

    def test_empty_list_means_no_restaurants(self):
        restrict(self.man)
        self.assertEqual(self.get('/api/bootstrap', client=self.mc)['restaurants'], [])

    def test_guests_of_other_restaurants_are_hidden(self):
        self.book(name='Анна', phone='+7 900 111-22-33')                      # гостья первого ресторана
        theirs = Guest.objects.get(phone_key='9001112233')
        mine = self.post(f'/api/restaurants/{self.second.id}/bookings', self.data(name='Борис', phone='+7 900 222-33-44', table=None),
                         client=self.login(self.boss), expect=400)
        self.assertIn('error', mine)  # во втором ресторане нет столов — добавим один и запишем Бориса
        from booking.models import Hall, Table
        hall = Hall.objects.create(restaurant=self.second, name='Зал')
        table = Table.objects.create(restaurant=self.second, hall=hall, number=1, seats=4, min_guests=1)
        self.post(f'/api/restaurants/{self.second.id}/bookings', self.data(name='Борис', phone='+7 900 222-33-44', table=table.id),
                  client=self.mc)
        listed = self.get('/api/guests', client=self.mc)
        self.assertEqual([g['name'] for g in listed['guests']], ['Борис'])
        self.get(f'/api/guests/{theirs.id}', client=self.mc, expect=404)
        self.assertIsNone(self.get('/api/guests/lookup?phone=9001112233', client=self.mc)['guest'])
        self.assertEqual(self.get('/api/guests/lookup?name=Ан', client=self.mc)['matches'], [])
        boris = Guest.objects.get(phone_key='9002223344')
        self.post(f'/api/guests/{boris.id}', {'black': True}, client=self.mc, expect=403)
        self.post(f'/api/guests/{boris.id}', {'op': 'erase'}, client=self.mc, expect=403)
        self.assertEqual(self.post(f'/api/guests/{boris.id}', {'note': 'любит у окна'}, client=self.mc)['guest']['note'], 'любит у окна')
        self.post('/api/tags', {'name': 'Своя'}, client=self.mc, expect=403)
        csv = self.mc.get('/api/export/guests.csv').content.decode('utf-8-sig')
        self.assertIn('Борис', csv)
        self.assertNotIn('Анна', csv)

    def test_visits_in_other_restaurants_are_not_counted(self):
        b = self.book(name='Анна', phone='+7 900 111-22-33', start=13 * 60)
        self.op(b['id'], 'status', status='seated')
        self.op(b['id'], 'status', status='done')
        guest = Guest.objects.get(phone_key='9001112233')
        guest.visits_before = 5
        guest.save()
        from booking.models import Hall, Table
        hall = Hall.objects.create(restaurant=self.second, name='Зал')
        table = Table.objects.create(restaurant=self.second, hall=hall, number=1, seats=4, min_guests=1)
        self.post(f'/api/restaurants/{self.second.id}/bookings', self.data(name='Анна', phone='+7 900 111-22-33', table=table.id),
                  client=self.mc)
        card = self.get(f'/api/guests/{guest.id}', client=self.mc)
        self.assertEqual((card['guest']['visits'], len(card['history'])), (0, 1))
        self.assertEqual(self.get(f'/api/guests/{guest.id}')['guest']['visits'], 6)


class Login(Base):
    def test_attempts_count_before_the_password_check(self):
        c = Client(REMOTE_ADDR='10.1.1.1')
        for _ in range(8):
            self.assertContains(c.post('/login/', {'username': 'owner', 'password': 'не тот'}), 'не подходят')
        self.assertContains(c.post('/login/', {'username': 'owner', 'password': 'pw-owner-12345'}), 'Слишком много попыток')
        self.now += timedelta(minutes=11)
        self.assertEqual(c.post('/login/', {'username': 'owner', 'password': 'pw-owner-12345'}).status_code, 302)

    def test_one_address_cannot_try_many_logins(self):
        c = Client(REMOTE_ADDR='10.1.1.2')
        for n in range(32):
            c.post('/login/', {'username': f'user{n}', 'password': 'x'})
        self.assertContains(c.post('/login/', {'username': 'owner', 'password': 'pw-owner-12345'}), 'Слишком много попыток')

    def test_one_login_cannot_be_tried_from_many_addresses(self):
        for n in range(32):
            Client(REMOTE_ADDR=f'10.2.0.{n}').post('/login/', {'username': 'owner', 'password': 'x'})
        page = Client(REMOTE_ADDR='10.3.0.1').post('/login/', {'username': 'owner', 'password': 'pw-owner-12345'})
        self.assertContains(page, 'Слишком много попыток')

    def test_temporary_password_expires(self):
        issued = self.post('/api/staff', {'name': 'Анна', 'login': 'anna', 'role': 'admin'})['issued']
        self.now += timedelta(days=8)
        self.assertContains(Client().post('/login/', {'username': 'anna', 'password': issued['password']}), 'устарел')

    def test_admin_needs_a_real_password_and_counts_attempts(self):
        user = User.objects.get(username='owner')
        user.is_staff = user.is_superuser = True
        user.must_change_password = True
        user.save()
        c = self.login(user)
        self.assertEqual(c.get('/admin/').url, '/password/')  # с временным паролем в админку не пускаем
        robot = Client(REMOTE_ADDR='10.4.0.1')
        for _ in range(8):
            robot.post('/admin/login/', {'username': 'owner', 'password': 'x'})
        self.assertEqual(robot.post('/admin/login/', {'username': 'owner', 'password': 'x'}).status_code, 429)


class GuestPageAbuse(Base):
    def setUp(self):
        super().setUp()
        self.r.pick_mode = 'scheme'
        self.r.save()

    def hold(self, client, table, start=19 * 60, key=KEY, expect=200):
        return self.post('/api/public/rest/hold', {'key': key, 'table': self.t[table].id, 'date': TOMORROW.isoformat(), 'start': start,
                                                   'guests': 2}, client=client, expect=expect)

    def book(self, client, expect=200, **over):
        d = {'name': 'Пётр', 'phone': '+7 900 555-44-33', 'guests': 2, 'date': TOMORROW.isoformat(), 'start': 19 * 60, 'consent': True}
        d.update(over)
        res = client.post('/api/public/rest/book', data=json.dumps(d), content_type='application/json')
        self.assertEqual(res.status_code, expect, res.content.decode())
        return res.json()

    def test_holds_only_on_scheme_and_one_per_address(self):
        g = Client(REMOTE_ADDR='10.5.0.1')
        self.hold(g, 2)
        self.hold(g, 3, key='q' * 24)                       # тот же адрес, другой ключ: прежний стол освобождается
        self.assertEqual(list(TableHold.objects.values_list('table__number', flat=True)), [3])
        self.r.pick_mode = 'form'
        self.r.save()
        self.hold(g, 2, expect=400)

    def test_holds_per_restaurant_are_capped(self):
        n = 0
        for start in (12 * 60, 14 * 60, 16 * 60):
            for table in (1, 2, 3, 4):
                n += 1
                self.post('/api/public/rest/hold', {'key': f'{n:024d}', 'table': self.t[table].id, 'date': TOMORROW.isoformat(),
                                                    'start': start, 'guests': 3 if table == 4 else 2}, client=Client(REMOTE_ADDR=f'10.6.0.{n}'))
        res = self.hold(Client(REMOTE_ADDR='10.6.1.1'), 1, start=18 * 60, key='x' * 24, expect=400)
        self.assertEqual(res['code'], 'busy')

    def test_staff_is_not_blocked_by_a_guest_hold(self):
        g = Client(REMOTE_ADDR='10.7.0.1')
        self.hold(g, 2)
        self.post(f'/api/restaurants/{self.r.id}/bookings', self.data(table=self.t[2].id, date=TOMORROW.isoformat()))
        self.assertEqual(self.book(g, expect=400, table=self.t[2].id, key=KEY)['code'], 'busy')

    def test_answers_do_not_reveal_other_bookings_or_blacklist(self):
        self.book(Client(REMOTE_ADDR='10.8.0.1'))
        dup = self.book(Client(REMOTE_ADDR='10.8.0.2'), expect=400, start=20 * 60)
        self.assertNotIn('12.10', dup['error'])
        self.assertNotIn('19:00', dup['error'])
        Guest.objects.create(org=self.org, name='Глеб', phone='+7 900 999-99-99', phone_key='9009999999', blacklisted=True)
        blocked = self.book(Client(REMOTE_ADDR='10.8.0.3'), expect=400, phone='+7 900 999-99-99')
        self.assertNotIn('чёрн', blocked['error'])
        self.assertNotIn('недоступна', blocked['error'])

    def test_one_phone_cannot_be_flooded_from_many_addresses(self):
        self.r.phone_limit = False
        self.r.save()
        for n in range(5):
            self.book(Client(REMOTE_ADDR=f'10.9.0.{n}'), date=(TODAY + timedelta(days=n + 1)).isoformat())
        res = self.book(Client(REMOTE_ADDR='10.9.1.1'), expect=429, date=(TODAY + timedelta(days=7)).isoformat())
        self.assertIn('номер', res['error'])

    def test_scheme_outside_online_dates_is_empty(self):
        far = TODAY + timedelta(days=self.r.horizon_days + 5)
        self.assertEqual(self.get(f'/api/public/rest/tables?date={far}&start={19 * 60}&guests=2', client=Client())['tables'], [])
        self.assertEqual(self.get(f'/api/public/rest/tables?date={TOMORROW}&start={19 * 60 + 7}&guests=2', client=Client())['tables'], [])

    def test_guest_cannot_move_a_large_group(self):
        b = self.post(f'/api/restaurants/{self.r.id}/bookings', self.data(guests=12, table=self.t[4].id, extra=[self.t[3].id, self.t[2].id],
                                                                         date=TOMORROW.isoformat()))['bookings'][0]
        token = Booking.objects.get(pk=b['id']).token
        res = Client().post(f'/api/public/receipt/{token}', data=json.dumps({'op': 'move', 'date': TOMORROW.isoformat(), 'start': 13 * 60}),
                            content_type='application/json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('переносит ресторан', res.json()['error'])

    def test_answers_with_data_are_not_cached(self):
        self.assertEqual(Client().get('/api/public/rest/info')['Cache-Control'], 'no-store')
        self.assertEqual(self.c.get('/api/bootstrap')['Cache-Control'], 'no-store')
        self.assertEqual(self.c.get('/api/guests/999')['Cache-Control'], 'no-store')


class SmsSafety(Base):
    def test_numbers(self):
        cases = {'+7 900 123-45-67': '+79001234567', '8 (900) 123 45 67': '+79001234567', '9001234567': '+79001234567',
                 '+380 50 123 4567': '', '+1 555 123 4567': '', '+7 495 123-45-67': '', '12345': ''}
        self.assertEqual({p: sms.to_number(p) for p in cases}, cases)

    def test_foreign_number_gets_no_sms(self):
        self.book(phone='+380 50 123 4567')
        self.assertFalse(Message.objects.exists())

    def test_staff_does_not_see_the_guest_link(self):
        b = self.book()
        code = Booking.objects.get(pk=b['id']).code
        text = self.get(f'/api/bookings/{b["id"]}/messages')['messages'][0]['text']
        self.assertNotIn(code, text)
        self.assertIn('/r/••••••', text)
        log = self.get(f'/api/restaurants/{self.r.id}/messages')['log'][0]['text']
        self.assertNotIn(code, log)


class Leftovers(Base):
    def test_erase_also_clears_tags_reasons_and_history(self):
        b = self.book(tags=['День рождения'])
        self.op(b['id'], 'status', status='cancelled', reason='Попросила перезвонить Ольге, номер +7 900 000-00-00')
        guest = Guest.objects.get(phone_key='9001112233')
        self.post(f'/api/guests/{guest.id}', {'op': 'erase'})
        gone = Booking.objects.get(pk=b['id'])
        self.assertEqual((gone.tags, gone.cancel_reason), ([], ''))
        self.assertFalse(any('reason' in e.data for e in BookingEvent.objects.filter(booking=gone)))

    def test_plan_bomb_is_refused_before_unpacking(self):
        buf = io.BytesIO()
        Image.new('1', (9000, 9000)).save(buf, 'PNG')       # 80 мегапикселей в файле на несколько килобайт
        self.assertLess(len(buf.getvalue()), 100_000)
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            res = self.c.post(f'/api/halls/{self.hall.id}/plan', {'file': SimpleUploadedFile('plan.png', buf.getvalue(), content_type='image/png')})
        self.assertEqual(res.status_code, 400)

    def test_old_attempt_counters_are_cleaned(self):
        from booking.models import Throttle
        from booking.net import clear_expired, hit
        hit('test', 'x', 5, 60)
        self.now += timedelta(minutes=2)
        clear_expired()
        self.assertFalse(Throttle.objects.exists())
