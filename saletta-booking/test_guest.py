import json
from datetime import timedelta

from django.test import Client

from booking.models import Booking, ClosedDay, Guest, TableHold

from .base import TODAY, Base, at

KEY = 'k' * 24
TOMORROW = TODAY + timedelta(days=1)


class GuestPage(Base):
    def setUp(self):
        super().setUp()
        self.g = Client()

    def slots(self, day=TODAY, guests=2, extra=''):
        res = self.get(f'/api/public/rest/slots?date={day}&guests={guests}{extra}', client=self.g)
        return {s['m']: s['ok'] for s in res['slots']}

    def send(self, expect=200, **over):
        d = {'name': 'Пётр', 'phone': '+7 900 555-44-33', 'guests': 2, 'date': TOMORROW.isoformat(), 'start': 19 * 60, 'consent': True}
        d.update(over)
        res = self.g.post('/api/public/rest/book', data=json.dumps(d), content_type='application/json')
        self.assertEqual(res.status_code, expect, res.content.decode())
        return res.json()

    def test_page_and_info_need_no_login(self):
        self.assertEqual(self.g.get('/b/rest/').status_code, 200)
        self.assertEqual(self.g.get('/b/net-takogo/').status_code, 404)
        info = self.get('/api/public/rest/info', client=self.g)['restaurant']
        self.assertEqual((info['name'], info['today'], info['halls']), ('Ресторан', TODAY.isoformat(), []))
        self.assertNotIn('rules', info)

    def test_lead_time_hides_near_slots(self):
        slots = self.slots()
        self.assertEqual(min(slots), 15 * 60 + 30)  # сейчас 15:00, бронь закрывается за 30 минут: раннее время не показываем
        self.assertTrue(slots[15 * 60 + 30])
        self.assertEqual(max(slots), 21 * 60 + 30)  # последний слот за полтора часа до закрытия

    def test_cutoff_closed_day_and_horizon(self):
        self.r.cutoff_on, self.r.cutoff_min = True, 20 * 60
        self.r.save()
        self.assertEqual(max(self.slots()), 19 * 60 + 30)
        ClosedDay.objects.create(restaurant=self.r, date=TOMORROW, reason='Банкет')
        res = self.get(f'/api/public/rest/slots?date={TOMORROW}&guests=2', client=self.g)
        self.assertEqual((res['slots'], res['closed']), ([], 'Банкет'))
        self.assertEqual(self.slots(TODAY + timedelta(days=14)), {})
        self.send(expect=400)

    def test_slot_closes_when_no_table_fits(self):
        for n in (1, 2, 3):
            self.book(name=f'Гость {n}', phone=f'+7 900 111-00-0{n}', table=self.t[n].id, date=TOMORROW.isoformat())
        slots = self.slots(TOMORROW)
        self.assertFalse(slots[19 * 60])        # стол на шестерых двоим не предлагаем
        self.assertTrue(slots[21 * 60])
        self.assertTrue(self.slots(TOMORROW, guests=5)[19 * 60])

    def test_max_bookings_per_start_time(self):
        self.r.max_start_on, self.r.max_start = True, 2
        self.r.save()
        for n in (2, 3):
            self.book(name=f'Гость {n}', phone=f'+7 900 111-00-0{n}', table=self.t[n].id, date=TOMORROW.isoformat())
        slots = self.slots(TOMORROW)
        self.assertEqual((slots[19 * 60], slots[19 * 60 + 30]), (False, True))  # на 19:00 уже две брони; на 19:30 свободен стол 1
        self.assertTrue(slots[21 * 60])

    def test_booking_creates_request_for_staff(self):
        res = self.send(note='У окна')
        receipt = res['receipt']
        self.assertEqual((receipt['status'], receipt['table'], receipt['locked']), ('new', None, False))
        b = Booking.objects.get()
        self.assertEqual((b.source, b.unseen, b.table.number, b.dur_min, b.guest.name), ('site', True, 1, 120, 'Пётр'))
        self.assertEqual(b.events.get().data['consent'], True)
        staff = self.get(f'/api/restaurants/{self.r.id}/sync?from={TOMORROW}&to={TOMORROW}')
        self.assertEqual((staff['newCount'], staff['bookings'][0]['unseen']), (1, True))
        self.assertFalse(self.op(b.id, 'seen')['bookings'][0]['unseen'])

    def test_refusals(self):
        self.send(expect=400, name='')
        self.send(expect=400, phone='12345')
        self.send(expect=400, consent=False)
        self.send(expect=400, guests=11)
        self.send(expect=400, start=19 * 60 + 15)
        self.send(expect=400, website='http://spam')
        self.assertEqual(Booking.objects.count(), 0)

    def test_phone_limit_and_blacklist(self):
        self.send()
        self.assertEqual(self.send(expect=400, start=20 * 60)['code'], 'duplicate')
        self.r.phone_limit = False
        self.r.save()
        self.send(start=21 * 60)
        Guest.objects.filter(phone_key='9005554433').update(blacklisted=True)
        res = self.send(expect=400, date=(TODAY + timedelta(days=2)).isoformat())
        self.assertEqual(res['code'], 'blocked')
        self.assertIn('+7 900 000-10-01', res['error'])

    def test_too_many_requests_from_one_address(self):
        self.r.phone_limit = False
        self.r.save()
        for n in range(6):
            self.send(phone=f'+7 900 555-00-0{n}', date=(TODAY + timedelta(days=n + 1)).isoformat())
        self.assertEqual(self.send(expect=429, phone='+7 900 555-00-09')['code'], 'throttled')

    def test_scheme_pick_and_hold(self):
        self.r.pick_mode = 'scheme'
        self.r.save()
        self.assertEqual(len(self.get('/api/public/rest/info', client=self.g)['restaurant']['halls'][0]['tables']), 4)
        q = f'date={TOMORROW}&start={19 * 60}&guests=2'
        states = {t['id']: t for t in self.get(f'/api/public/rest/tables?{q}', client=self.g)['tables']}
        self.assertEqual((states[self.t[1].id], states[self.t[4].id]['fits']), ({'id': self.t[1].id, 'free': True, 'fits': True}, False))
        hold = {'key': KEY, 'table': self.t[2].id, 'date': TOMORROW.isoformat(), 'start': 19 * 60, 'guests': 2}
        self.post('/api/public/rest/hold', hold, client=self.g)
        other = Client(REMOTE_ADDR='10.0.0.9')
        self.post('/api/public/rest/hold', {**hold, 'key': 'z' * 24}, client=other, expect=400)       # другой гость тот же стол не возьмёт
        others = {t['id']: t['free'] for t in self.get(f'/api/public/rest/tables?{q}&key={"z" * 24}', client=other)['tables']}
        self.assertEqual((others[self.t[2].id], others[self.t[3].id]), (False, True))
        staff = self.get(f'/api/restaurants/{self.r.id}/sync?from={TOMORROW}&to={TOMORROW}')
        self.assertEqual(staff['holds'], [{'table': self.t[2].id, 'date': TOMORROW.isoformat(), 'start': 19 * 60, 'dur': 120}])
        receipt = self.send(table=self.t[2].id, key=KEY)['receipt']
        self.assertEqual(receipt['table'], {'num': 2, 'hall': 'Зал'})
        self.assertFalse(TableHold.objects.exists())
        self.assertTrue(Booking.objects.get().picked)

    def test_hold_expires(self):
        self.r.pick_mode = 'scheme'
        self.r.save()
        hold = {'key': KEY, 'table': self.t[2].id, 'date': TOMORROW.isoformat(), 'start': 19 * 60, 'guests': 2}
        self.post('/api/public/rest/hold', hold, client=self.g)
        self.now = at(15, 11)
        self.book(table=self.t[2].id, date=TOMORROW.isoformat())

    def test_guest_cancels_and_staff_sees_it(self):
        token = self.send()['receipt']['token']
        url = f'/api/public/receipt/{token}'
        self.assertEqual(self.get(url, client=self.g)['receipt']['status'], 'new')
        self.assertEqual(self.post(url, {'op': 'cancel'}, client=self.g)['receipt']['status'], 'cancelled')
        b = Booking.objects.get()
        self.assertEqual((b.cancel_reason, b.guest_cancelled, b.unseen), ('Гость отменил сам', True, True))
        self.post(url, {'op': 'cancel'}, client=self.g, expect=400)
        self.get('/api/public/receipt/net-takoi', client=self.g, expect=404)

    def test_guest_moves_and_keeps_table_when_free(self):
        token = self.send()['receipt']['token']
        day = TODAY + timedelta(days=2)
        self.assertTrue(self.slots(TOMORROW, extra=f'&token={token}')[19 * 60])
        res = self.post(f'/api/public/receipt/{token}', {'op': 'move', 'date': day.isoformat(), 'start': 20 * 60}, client=self.g)['receipt']
        self.assertEqual((res['date'], res['start'], res['moved']), (day.isoformat(), 20 * 60, True))
        self.assertEqual(Booking.objects.get().table.number, 1)

    def test_moves_do_not_resend_reminders_endlessly(self):
        from booking.models import Message, MessageKind
        token = self.send()['receipt']['token']
        b = Booking.objects.get()
        url = f'/api/public/receipt/{token}'
        day = TODAY + timedelta(days=2)
        Message.objects.create(restaurant=b.restaurant, booking=b, kind=MessageKind.REMIND, phone=b.phone, text='1')
        self.post(url, {'op': 'move', 'date': day.isoformat(), 'start': 20 * 60}, client=self.g)
        self.assertFalse(Booking.objects.get().reminded)  # после первого напоминания перенос даёт ещё одно
        Message.objects.create(restaurant=b.restaurant, booking=b, kind=MessageKind.REMIND, phone=b.phone, text='2')
        self.post(url, {'op': 'move', 'date': day.isoformat(), 'start': 19 * 60}, client=self.g)
        self.assertTrue(Booking.objects.get().reminded)  # дальше переносы новых напоминаний не дают

    def test_guest_cannot_change_shortly_before_visit(self):
        token = self.send(date=TODAY.isoformat(), start=16 * 60)['receipt']['token']
        url = f'/api/public/receipt/{token}'
        self.assertFalse(self.get(url, client=self.g)['receipt']['locked'])
        self.now = at(15, 10)
        self.assertTrue(self.get(url, client=self.g)['receipt']['locked'])
        self.assertEqual(self.post(url, {'op': 'cancel'}, client=self.g, expect=400)['code'], 'locked')

    def test_receipt_shows_nothing_internal(self):
        receipt = self.send()['receipt']
        self.assertEqual(set(receipt), {'id', 'token', 'status', 'date', 'start', 'dur', 'guests', 'name', 'picked', 'moved', 'come', 'table', 'locked', 'review', 'tg'})
