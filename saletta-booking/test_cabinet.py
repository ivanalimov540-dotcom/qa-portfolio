from datetime import timedelta

from booking.models import Booking, BookingEvent, Guest, Role, Table, WaitEntry

from .base import TODAY, Base, at


class BookingRules(Base):
    def test_create_links_guest_and_logs(self):
        b = self.book()
        self.assertEqual((b['status'], b['source'], b['table']), ('confirmed', 'phone', self.t[2].id))
        guest = Guest.objects.get(org=self.org)
        self.assertEqual((guest.phone_key, b['gid']), ('9001112233', guest.id))
        self.assertEqual(BookingEvent.objects.get().action, 'create')

    def test_same_table_same_time_is_refused(self):
        self.book()
        res = self.post(f'/api/restaurants/{self.r.id}/bookings', self.data(name='Борис', phone='+7 900 222-33-44', start=20 * 60), expect=400)
        self.assertEqual(res['code'], 'busy')
        self.assertIn('Стол 2 занят', res['error'])

    def test_back_to_back_is_allowed_until_buffer_applies(self):
        self.book()
        self.book(name='Борис', phone='+7 900 222-33-44', start=21 * 60, dur=60)
        self.r.buffer_min, self.r.buffer_admin = 30, True
        self.r.save()
        self.post(f'/api/restaurants/{self.r.id}/bookings', self.data(name='Вера', phone='+7 900 333-44-55', start=17 * 60), expect=400)

    def test_group_booking_takes_all_its_tables(self):
        b = self.book(guests=8, table=self.t[4].id, extra=[self.t[3].id])
        self.assertEqual(b['extra'], [self.t[3].id])
        self.post(f'/api/restaurants/{self.r.id}/bookings', self.data(name='Борис', phone='+7 900 222-33-44', table=self.t[3].id), expect=400)

    def test_validation(self):
        url = f'/api/restaurants/{self.r.id}/bookings'
        for bad in ({'name': ''}, {'phone': '123'}, {'guests': 0}, {'date': 'вчера'}, {'date': (TODAY - timedelta(days=1)).isoformat()},
                    {'start': 11 * 60}, {'start': 22 * 60, 'dur': 120}, {'start': 19 * 60 + 7}, {'table': 999999}, {'extra': [999999]}):
            self.post(url, self.data(**bad), expect=400)
        self.assertEqual(Booking.objects.count(), 0)

    def test_walkin_needs_no_phone_and_is_seated(self):
        b = self.book(name='', phone='', walkin=True, start=15 * 60)
        self.assertEqual((b['name'], b['status'], b['source']), ('Гость без брони', 'seated', 'walkin'))

    def test_status_flow_and_cancel_reason(self):
        b = self.book()
        self.op(b['id'], 'status', expect=400, status='done')
        self.op(b['id'], 'status', expect=400, status='cancelled')
        res = self.op(b['id'], 'status', status='cancelled', reason='Дубль брони')['bookings'][0]
        self.assertEqual((res['status'], res['why']), ('cancelled', 'Дубль брони'))
        res = self.op(b['id'], 'status', status='confirmed')['bookings'][0]
        self.assertEqual((res['status'], res['why']), ('confirmed', ''))
        res = self.op(b['id'], 'status', status='cancelled', reason='Гость не пришёл')['bookings'][0]
        self.assertEqual(res['status'], 'noshow')

    def test_restore_is_refused_when_table_was_taken(self):
        b = self.book()
        self.op(b['id'], 'status', status='cancelled', reason='Дубль брони')
        self.book(name='Борис', phone='+7 900 222-33-44')
        self.assertEqual(self.op(b['id'], 'status', expect=400, status='confirmed')['code'], 'busy')

    def test_cancel_without_reason_when_not_required(self):
        self.r.cancel_required = False
        self.r.save()
        b = self.book()
        self.assertEqual(self.op(b['id'], 'status', status='cancelled')['bookings'][0]['status'], 'cancelled')

    def test_shift(self):
        b = self.book()
        self.book(name='Борис', phone='+7 900 222-33-44', start=21 * 60, dur=60)
        self.assertEqual(self.op(b['id'], 'shift', delta=-60)['bookings'][0]['start'], 18 * 60)
        self.op(b['id'], 'shift', delta=120, expect=400)   # упирается во вторую бронь
        self.op(b['id'], 'shift', delta=7, expect=400)     # только с шагом 15 минут
        self.op(b['id'], 'shift', delta=-420, expect=400)  # раньше открытия

    def test_move_and_swap(self):
        a = self.book()
        b = self.book(name='Борис', phone='+7 900 222-33-44', table=self.t[3].id, start=20 * 60)
        self.op(a['id'], 'move', table=self.t[3].id, expect=400)
        self.assertEqual(self.op(a['id'], 'move', table=self.t[1].id)['bookings'][0]['table'], self.t[1].id)
        res = self.op(a['id'], 'swap', other=b['id'])['bookings']
        self.assertEqual({x['id']: x['table'] for x in res}, {a['id']: self.t[3].id, b['id']: self.t[1].id})

    def test_extend_only_while_seated_and_free(self):
        b = self.book(start=18 * 60)
        self.op(b['id'], 'extend', expect=400)
        self.op(b['id'], 'status', status='seated')
        self.assertEqual(self.op(b['id'], 'extend')['bookings'][0]['dur'], 150)
        self.book(name='Борис', phone='+7 900 222-33-44', start=20 * 60 + 30, dur=60)
        self.assertEqual(self.op(b['id'], 'extend', expect=400)['code'], 'busy')

    def test_update_keeps_own_slot_and_checks_others(self):
        a = self.book()
        self.book(name='Борис', phone='+7 900 222-33-44', table=self.t[3].id)
        res = self.op(a['id'], 'update', **self.data(guests=3, note='У окна', tags=['VIP']))['bookings'][0]
        self.assertEqual((res['guests'], res['note'], res['tags']), (3, 'У окна', ['VIP']))
        self.op(a['id'], 'update', expect=400, **self.data(table=self.t[3].id))

    def test_auto_noshow(self):
        b = self.book(start=14 * 60)
        self.assertEqual(self.sync()['bookings'][0]['status'], 'confirmed')
        self.r.auto_noshow = True
        self.r.save()
        res = self.sync()
        self.assertEqual((res['swept'], res['bookings'][0]['status']), (1, 'noshow'))
        self.assertIn('Автоотмена', Booking.objects.get(pk=b['id']).cancel_reason)

    def sync(self, **params):
        query = '&'.join(f'{k}={v}' for k, v in params.items()) or f'from={TODAY}&to={TODAY}'
        return self.get(f'/api/restaurants/{self.r.id}/sync?{query}')

    def test_sync_window_and_changes(self):
        self.book()
        self.book(name='Борис', phone='+7 900 222-33-44', date=(TODAY + timedelta(days=5)).isoformat())
        first = self.sync()
        self.assertEqual((len(first['bookings']), first['today'], first['now']), (1, TODAY.isoformat(), 15 * 60))
        self.assertEqual(list(first['guests'].values())[0]['seg'], 'none')
        self.now = at(15, 5)
        later = self.book(name='Вера', phone='+7 900 333-44-55', table=self.t[3].id)
        changed = self.sync(since=at(15, 4).isoformat().replace('+', '%2B'))
        self.assertEqual([b['id'] for b in changed['bookings']], [later['id']])

    def test_business_day_after_midnight(self):
        self.r.close_min = 26 * 60
        self.r.save()
        self.now = at(1, 30, TODAY + timedelta(days=1))
        res = self.sync()
        self.assertEqual((res['today'], res['now']), (TODAY.isoformat(), 25 * 60 + 30))

    def test_waitlist(self):
        entry = self.post(f'/api/restaurants/{self.r.id}/wait', {'name': 'Игорь', 'date': TODAY.isoformat(), 'start': 1200, 'guests': 4})['entry']
        self.book(wait=entry['id'])
        self.assertFalse(WaitEntry.objects.exists())


class Access(Base):
    def test_anonymous_gets_401_and_login_page(self):
        from django.test import Client
        anon = Client()
        self.assertEqual(anon.get('/api/bootstrap').status_code, 401)
        self.assertEqual(anon.get('/').status_code, 302)

    def test_roles(self):
        admin, viewer, manager = (self.login(self.member(n, r)) for n, r in (('adm', Role.ADMIN), ('view', Role.VIEWER), ('man', Role.MANAGER)))
        url = f'/api/restaurants/{self.r.id}/bookings'
        self.post(url, self.data(), client=viewer, expect=403)
        b = self.post(url, self.data(), client=admin)['bookings'][0]
        self.op(b['id'], 'status', client=viewer, expect=403, status='seated')
        self.assertTrue(self.get(f'/api/restaurants/{self.r.id}/sync?from={TODAY}&to={TODAY}', client=viewer)['bookings'])
        self.post(f'/api/restaurants/{self.r.id}', {'lead_min': 60}, client=admin, expect=403)
        self.post(f'/api/restaurants/{self.r.id}', {'lead_min': 60}, client=manager)
        self.post('/api/restaurants', {'name': 'Новый', 'open': 720, 'close': 1380, 'n2': 1, 'n4': 0, 'n6': 0}, client=manager, expect=403)

    def test_other_network_sees_nothing(self):
        from booking.api import new_organization
        from booking.models import Restaurant
        from .base import User
        stranger = User.objects.create_user('stranger', password='pw-stranger-12345')
        other = new_organization('Чужая сеть', stranger)
        Restaurant.objects.create(org=other, name='Чужой', slug='other')
        c = self.login(stranger)
        b = self.book()
        guest = Guest.objects.get(org=self.org)
        self.assertEqual([r['name'] for r in self.get('/api/bootstrap', client=c)['restaurants']], ['Чужой'])
        self.get(f'/api/restaurants/{self.r.id}/sync?from={TODAY}&to={TODAY}', client=c, expect=404)
        self.post(f'/api/restaurants/{self.r.id}/bookings', self.data(), client=c, expect=404)
        self.op(b['id'], 'status', client=c, expect=404, status='seated')
        self.get(f'/api/guests/{guest.id}', client=c, expect=404)
        self.assertEqual(self.get('/api/guests', client=c)['total'], 0)
        self.post(f'/api/tables/{self.t[1].id}', {'op': 'delete'}, client=c, expect=404)
        self.post(f'/api/halls/{self.hall.id}/tables', {'seats': 2}, client=c, expect=404)

    def test_member_limited_to_one_restaurant(self):
        from booking.models import Membership, Restaurant
        second = Restaurant.objects.create(org=self.org, name='Второй', slug='second')
        user = self.member('adm', Role.ADMIN)
        m = Membership.objects.get(user=user)
        m.all_restaurants = False
        m.save()
        m.restaurants.add(second)
        c = self.login(user)
        self.assertEqual([r['name'] for r in self.get('/api/bootstrap', client=c)['restaurants']], ['Второй'])
        self.get(f'/api/restaurants/{self.r.id}/sync?from={TODAY}&to={TODAY}', client=c, expect=404)

    def test_csrf_is_enforced(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.owner)
        res = c.post(f'/api/restaurants/{self.r.id}/bookings', data='{}', content_type='application/json')
        self.assertEqual(res.status_code, 403)


class Guests(Base):
    def test_list_search_segments_and_card(self):
        Guest.objects.create(org=self.org, name='Ольга В.', phone='+7 900 000-00-05', phone_key='9000000005', visits_before=9,
                             last_visit_before=TODAY - timedelta(days=3), vip=True)
        Guest.objects.create(org=self.org, name='Глеб Ж.', phone='+7 900 000-00-13', phone_key='9000000013', visits_before=1,
                             noshows_before=4, last_visit_before=TODAY - timedelta(days=95), blacklisted=True)
        b = self.book(name='Ольга В.', phone='+7 900 000-00-05')
        res = self.get('/api/guests')
        self.assertEqual((res['total'], res['counts']['regular'], res['counts']['lost'], res['counts']['vip'], res['counts']['black']), (2, 1, 1, 1, 1))
        self.assertEqual([g['name'] for g in self.get('/api/guests?q=ольга')['guests']], ['Ольга В.'])
        self.assertEqual([g['name'] for g in self.get('/api/guests?q=0013')['guests']], ['Глеб Ж.'])
        self.assertEqual([g['name'] for g in self.get('/api/guests?seg=black')['guests']], ['Глеб Ж.'])
        card = self.get(f'/api/guests/{b["gid"]}')
        self.assertEqual((card['guest']['visits'], card['guest']['last'], len(card['history'])), (9, 3, 1))
        self.op(b['id'], 'status', status='seated')
        self.op(b['id'], 'status', status='done')
        self.assertEqual(self.get(f'/api/guests/{b["gid"]}')['guest']['visits'], 10)
        self.assertTrue(self.post(f'/api/guests/{b["gid"]}', {'black': True, 'vip': False})['guest']['black'])
        self.assertEqual(self.get('/api/guests/lookup?phone=%2B7 900 000-00-05')['guest']['name'], 'Ольга В.')
        self.assertEqual(self.get('/api/guests/lookup?name=гле')['matches'][0]['name'], 'Глеб Ж.')
        self.assertIsNone(self.get('/api/guests/lookup?phone=%2B7 999 999-99-99')['guest'])


class Settings(Base):
    def test_rules_are_validated(self):
        url = f'/api/restaurants/{self.r.id}'
        res = self.post(url, {'lead_min': 120, 'phone_limit': False, 'pick': 'scheme', 'name': 'Новое имя', 'close': 26 * 60})['restaurant']
        self.assertEqual((res['rules']['lead_min'], res['rules']['phone_limit'], res['pick'], res['name'], res['close']), (120, False, 'scheme', 'Новое имя', 1560))
        for bad in ({'lead_min': 7}, {'phone_limit': 'да'}, {'max_start': True}, {'pick': 'x'}, {'name': ' '}, {'open': 720, 'close': 780}):
            self.post(url, bad, expect=400)

    def test_closed_days_and_tags(self):
        day = (TODAY + timedelta(days=3)).isoformat()
        res = self.post(f'/api/restaurants/{self.r.id}/closed', {'date': day, 'reason': 'Банкет'})['restaurant']
        self.assertEqual(res['closed'], [{'date': day, 'reason': 'Банкет'}])
        self.post(f'/api/restaurants/{self.r.id}/closed', {'date': day}, expect=400)
        self.assertEqual(self.post(f'/api/restaurants/{self.r.id}/closed/delete', {'date': day})['restaurant']['closed'], [])
        self.assertIn('Юбилей', self.post('/api/tags', {'name': 'Юбилей'})['tags'])
        self.post('/api/tags', {'name': 'юбилей'}, expect=400)
        self.assertNotIn('Юбилей', self.post('/api/tags/delete', {'name': 'Юбилей'})['tags'])
        self.assertIn('Не пересаживать', self.post('/api/tags/delete', {'name': 'Не пересаживать'})['tags'])

    def test_staff_gets_temporary_password_and_must_change_it(self):
        from django.test import Client
        res = self.post('/api/staff', {'name': 'Анна', 'login': 'Anna.Zal', 'role': 'admin'})
        issued = res['issued']
        self.assertEqual((issued['login'], len(res['staff'])), ('anna.zal', 2))
        self.post('/api/staff', {'name': 'Анна', 'login': 'anna.zal', 'role': 'admin'}, expect=400)
        self.post('/api/staff', {'name': 'Анна', 'login': 'аня', 'role': 'admin'}, expect=400)
        c = Client()
        page = c.post('/login/', {'username': 'anna.zal', 'password': issued['password']})
        self.assertEqual(page.status_code, 302)
        self.assertEqual(c.get('/').url, '/password/')
        self.assertEqual(c.get('/api/bootstrap').status_code, 403)
        done = c.post('/password/', {'old_password': issued['password'], 'new_password1': 'novyi-parol-2026', 'new_password2': 'novyi-parol-2026'})
        self.assertEqual(done.status_code, 302)
        self.assertEqual(c.get('/api/bootstrap').status_code, 200)

    def test_staff_roles_reset_and_removal(self):
        manager = self.member('man', Role.MANAGER)
        mc = self.login(manager)
        owner_row = self.get('/api/staff')['staff'][0]
        self.post(f'/api/staff/{owner_row["id"]}', {'op': 'remove'}, client=mc, expect=403)
        self.post('/api/staff', {'name': 'Ещё владелец', 'login': 'boss2', 'role': 'owner'}, client=mc, expect=400)
        row = self.post('/api/staff', {'name': 'Анна', 'login': 'anna', 'role': 'admin'}, client=mc)['staff'][-1]
        self.assertEqual(self.post(f'/api/staff/{row["id"]}', {'op': 'role', 'role': 'viewer'}, client=mc)['staff'][-1]['role'], 'viewer')
        self.assertIn('password', self.post(f'/api/staff/{row["id"]}', {'op': 'reset'}, client=mc)['issued'])
        self.post(f'/api/staff/{owner_row["id"]}', {'op': 'role', 'role': 'viewer'}, expect=403)  # себя менять нельзя
        self.assertEqual(len(self.post(f'/api/staff/{row["id"]}', {'op': 'remove'})['staff']), 2)
        from .base import User
        self.assertFalse(User.objects.get(username='anna').is_active)

    def test_login_lock_after_many_attempts(self):
        from django.test import Client
        c = Client()
        for _ in range(8):
            self.assertContains(c.post('/login/', {'username': 'owner', 'password': 'не тот'}), 'не подходят')
        self.assertContains(c.post('/login/', {'username': 'owner', 'password': 'pw-owner-12345'}), 'Слишком много попыток')

    def test_foreign_attempts_do_not_lock_owner_on_known_address(self):
        from django.test import Client
        home = Client(REMOTE_ADDR='10.1.1.1')
        self.assertEqual(home.post('/login/', {'username': 'owner', 'password': 'pw-owner-12345'}).status_code, 302)
        home.post('/logout/')
        for i in range(40):  # перебор логина с разных адресов
            Client(REMOTE_ADDR=f'10.9.0.{i}').post('/login/', {'username': 'owner', 'password': 'не тот'})
        self.assertContains(Client(REMOTE_ADDR='10.7.7.7').post('/login/', {'username': 'owner', 'password': 'pw-owner-12345'}), 'Слишком много попыток')
        self.assertEqual(home.post('/login/', {'username': 'owner', 'password': 'pw-owner-12345'}).status_code, 302)


class HallEditor(Base):
    def test_wizard_creates_restaurant_with_layout(self):
        res = self.post('/api/restaurants', {'name': 'Кафе у парка', 'addr': 'Парковая, 3', 'open': 600, 'close': 1380, 'n2': 4, 'n4': 4, 'n6': 2})['restaurant']
        tables = res['halls'][0]['tables']
        self.assertEqual((res['slug'], len(tables), [t['num'] for t in tables][:3]), ('kafe-u-parka', 10, [1, 2, 3]))
        self.assertTrue(all(0 <= t['x'] and t['x'] + t['w'] <= 100 and t['y'] + t['h'] <= 100 for t in tables))
        again = self.post('/api/restaurants', {'name': 'Кафе у парка', 'open': 600, 'close': 1380, 'n2': 1, 'n4': 0, 'n6': 0})['restaurant']
        self.assertEqual(again['slug'], 'kafe-u-parka-2')
        self.post('/api/restaurants', {'name': 'Пусто', 'open': 600, 'close': 1380, 'n2': 0, 'n4': 0, 'n6': 0}, expect=400)

    def test_tables(self):
        res = self.post(f'/api/halls/{self.hall.id}/tables', {'seats': 8})
        new = next(t for t in res['restaurant']['halls'][0]['tables'] if t['id'] == res['table'])
        self.assertEqual((new['num'], new['seats'], new['min'], new['w']), (5, 8, 5, 36))
        url = f'/api/tables/{new["id"]}'
        moved = self.post(url, {'op': 'move', 'x': 90.3, 'y': -5})['restaurant']['halls'][0]['tables'][-1]
        self.assertEqual((moved['x'], moved['y']), (64, 0))
        self.post(url, {'op': 'number', 'number': 1}, expect=400)
        self.post(url, {'op': 'number', 'number': 21})
        self.post(url, {'op': 'seats', 'seats': 2})
        self.assertEqual(Table.objects.get(pk=new['id']).round, True)
        self.post(url, {'op': 'delete'})
        self.assertFalse(Table.objects.filter(pk=new['id']).exists())

    def test_table_with_bookings_is_protected_then_hidden(self):
        b = self.book()
        url = f'/api/tables/{self.t[2].id}'
        self.assertIn('действующие брони', self.post(url, {'op': 'delete'}, expect=400)['error'])
        self.op(b['id'], 'status', status='cancelled', reason='Дубль брони')
        tables = self.post(url, {'op': 'delete'})['restaurant']['halls'][0]['tables']
        self.assertNotIn(self.t[2].id, [t['id'] for t in tables])
        self.assertFalse(Table.objects.get(pk=self.t[2].id).active)
        self.post(f'/api/halls/{self.hall.id}/tables', {'seats': 4})  # номер 2 теперь свободен для нового стола? нет: берётся следующий
        self.post(f'/api/restaurants/{self.r.id}/bookings', self.data(name='Борис', phone='+7 900 222-33-44'), expect=400)

    def test_halls(self):
        res = self.post(f'/api/restaurants/{self.r.id}/halls', {'name': 'Веранда'})
        self.assertEqual([h['name'] for h in res['restaurant']['halls']], ['Зал', 'Веранда'])
        self.post(f'/api/halls/{res["hall"]}', {'op': 'rename', 'name': 'Терраса'})
        self.post(f'/api/halls/{self.hall.id}', {'op': 'delete'}, expect=400)
        self.assertEqual(len(self.post(f'/api/halls/{res["hall"]}', {'op': 'delete'})['restaurant']['halls']), 1)
        self.post(f'/api/halls/{self.hall.id}', {'op': 'delete'}, expect=400)

    def test_plan_upload_accepts_images_only(self):
        import io
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (3000, 1500), 'white').save(buf, 'PNG')
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            url = f'/api/halls/{self.hall.id}/plan'
            junk = self.c.post(url, {'file': SimpleUploadedFile('plan.png', b'<script>alert(1)</script>', content_type='image/png')})
            self.assertEqual(junk.status_code, 400)
            ok = self.c.post(url, {'file': SimpleUploadedFile('plan.png', buf.getvalue(), content_type='image/png')}).json()
            bg = ok['restaurant']['halls'][0]['bg']
            self.assertTrue(bg.endswith('.jpg'))
            self.hall.refresh_from_db()
            with Image.open(self.hall.plan.path) as saved:
                self.assertEqual(saved.size, (2000, 1000))
            self.assertIsNone(self.post(url, {'op': 'delete'})['restaurant']['halls'][0]['bg'])


class GuestSearch(Base):
    def test_phone_with_eight_and_letter_yo(self):
        self.book(name='Артём', phone='+7 900 111-22-33')
        from urllib.parse import quote
        for q in ('89001112233', '8 900 111-22-33', '+7 900 111-22-33', 'артем', 'АРТЁМ', '2233'):
            self.assertEqual(self.get('/api/guests?q=' + quote(q))['total'], 1, q)
        self.assertEqual(self.get('/api/guests?q=' + quote('79001112234'))['total'], 0)
