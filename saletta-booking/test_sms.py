import json
from datetime import timedelta

from django.test import Client, override_settings

from booking import sms
from booking.models import Booking, Message, Role

from .base import TODAY, Base, at

TOMORROW = TODAY + timedelta(days=1)


class Messages(Base):
    def kinds(self):
        return list(Message.objects.order_by('id').values_list('kind', flat=True))

    def test_staff_booking_sends_confirmation_with_short_link(self):
        b = self.book()
        m = Message.objects.get()
        code = Booking.objects.get(pk=b['id']).code
        self.assertEqual((m.kind, m.status, m.phone, m.parts), ('confirmed', 'test', '+79001112233', 2))
        self.assertEqual(m.text, f'Ресторан: бронь на 11 октября, 19:00, 2 гостя подтверждена. Изменить или отменить: http://localhost:8765/r/{code}')
        self.book(name='', phone='', walkin=True, start=15 * 60, table=self.t[3].id)
        self.assertEqual(Message.objects.count(), 1)

    def test_status_changes(self):
        b = self.book()
        self.op(b['id'], 'shift', delta=30)
        self.op(b['id'], 'status', status='cancelled', reason='Дубль брони')
        self.op(b['id'], 'status', status='confirmed')
        self.op(b['id'], 'status', status='seated')
        self.op(b['id'], 'status', status='confirmed')   # «пришёл» нажали по ошибке — подтверждение второй раз не уходит
        self.assertEqual(self.kinds(), ['confirmed', 'changed', 'cancelled', 'confirmed'])
        self.assertIn('перенесена на 11 октября, 19:30', Message.objects.get(kind='changed').text)

    def test_guest_request_then_confirmation(self):
        # Заявка с сайта по умолчанию без SMS: номер в ней ещё никто не проверил.
        self.assertFalse(next(t for t in sms.templates(self.r) if t.kind == 'request').enabled)
        self.post(f'/api/restaurants/{self.r.id}/templates', {'kind': 'request', 'enabled': True})
        g = Client()
        res = g.post('/api/public/rest/book', data=json.dumps({'name': 'Пётр', 'phone': '+7 900 555-44-33', 'guests': 2, 'date': TOMORROW.isoformat(),
                                                             'start': 19 * 60, 'consent': True}), content_type='application/json').json()
        b = Booking.objects.get()
        self.op(b.id, 'status', status='confirmed')
        g.post(f'/api/public/receipt/{res["receipt"]["token"]}', data='{"op": "cancel"}', content_type='application/json')
        self.assertEqual(self.kinds(), ['request', 'confirmed'])
        self.assertIn('заявка на 12 октября, 19:00, 2 гостя принята', Message.objects.get(kind='request').text)

    def test_templates(self):
        url = f'/api/restaurants/{self.r.id}/templates'
        self.post(url, {'kind': 'confirmed', 'enabled': False})
        self.book()
        self.assertEqual(Message.objects.count(), 0)
        self.assertIn('Такой подстановки нет: {сумма}', self.post(url, {'kind': 'remind', 'text': 'Ждём {имя}, {сумма}'}, expect=400)['error'])
        state = self.post(url, {'kind': 'remind', 'text': 'Ждём вас, {имя}! {ссылка}'})
        self.assertEqual(next(t for t in state['templates'] if t['kind'] == 'remind')['text'], 'Ждём вас, {имя}! {ссылка}')
        state = self.post(url, {'kind': 'remind', 'reset': True})
        self.assertEqual(next(t for t in state['templates'] if t['kind'] == 'remind')['text'], sms.DEFAULTS['remind'])
        self.post(url, {'kind': 'nope'}, expect=400)
        self.assertEqual(state['mode'], 'test')

    def test_reminder_once_at_the_right_time(self):
        b = self.book(date=TOMORROW.isoformat())             # записали за сутки
        self.now = at(15, 59, TOMORROW)
        self.assertEqual(sms.queue_reminders(self.r), 0)
        self.now = at(16, 0, TOMORROW)                        # за 3 часа до 19:00
        self.get(f'/api/restaurants/{self.r.id}/sync?from={TOMORROW}&to={TOMORROW}')
        self.assertEqual(sms.queue_reminders(self.r), 0)
        m = Message.objects.get(kind='remind')
        self.assertTrue(m.text.startswith('Анна, ждём вас сегодня в 19:00, Ресторан.'))
        self.assertTrue(Booking.objects.get(pk=b['id']).reminded)
        self.op(b['id'], 'shift', delta=60)                   # время сменилось — напомним ещё раз к новому
        self.now = at(17, 0, TOMORROW)
        self.assertEqual(sms.queue_reminders(self.r), 1)

    def test_no_reminders_at_night_or_for_last_minute_bookings(self):
        self.book(date=TOMORROW.isoformat(), start=12 * 60, table=self.t[1].id)
        self.now = at(8, 50, TOMORROW)                         # срок в 9:00 (12:00 минус 3 часа), а сейчас ещё ночные часы
        self.assertEqual(sms.queue_reminders(self.r), 0)
        self.now = at(9, 0, TOMORROW)
        self.assertEqual(sms.queue_reminders(self.r), 1)
        self.now = at(10, 0, TOMORROW)
        late = self.book(date=TOMORROW.isoformat(), start=12 * 60 + 30, table=self.t[3].id, phone='+7 900 222-33-44')  # за 2,5 часа
        self.assertEqual(sms.queue_reminders(self.r), 0)
        self.assertTrue(Booking.objects.get(pk=late['id']).reminded)

    def test_reminders_wait_for_morning(self):
        self.r.remind_hours = 24
        self.r.save()
        self.book(date=(TODAY + timedelta(days=2)).isoformat(), start=12 * 60)
        self.now = at(22, 0, TOMORROW)                         # срок — в 12:00 завтра, но сейчас ночь и до срока далеко
        self.assertEqual(sms.queue_reminders(self.r), 0)
        self.now = at(12, 0, TOMORROW)
        self.assertEqual(sms.queue_reminders(self.r), 1)

    def test_manual_reminder_and_guest_answer(self):
        b = self.book(date=TOMORROW.isoformat())
        self.op(b['id'], 'remind')
        self.assertEqual(self.kinds(), ['confirmed', 'remind'])
        token = Booking.objects.get(pk=b['id']).token
        res = Client().post(f'/api/public/receipt/{token}', data='{"op": "come"}', content_type='application/json').json()
        self.assertTrue(res['receipt']['come'])
        self.assertTrue(self.get(f'/api/restaurants/{self.r.id}/sync?from={TOMORROW}&to={TOMORROW}')['bookings'][0]['come'])
        self.assertIn('уже отправлено', self.op(b['id'], 'remind', expect=400)['error'])  # второй раз подряд не шлём
        self.now += timedelta(minutes=11)
        self.post(f'/api/restaurants/{self.r.id}/templates', {'kind': 'remind', 'enabled': False})
        self.assertIn('выключены', self.op(b['id'], 'remind', expect=400)['error'])

    def test_waitlist_message(self):
        self.post(f'/api/restaurants/{self.r.id}/wait', {'name': 'Игорь', 'phone': '+7 900 333-22-11', 'date': TODAY.isoformat(), 'start': 1200, 'guests': 4})
        self.assertEqual(Message.objects.get().text, 'Ресторан: вы в листе ожидания на 11 октября, 20:00. Позвоним, если освободится стол.')

    def test_short_link_opens_guest_page(self):
        b = Booking.objects.get(pk=self.book()['id'])
        res = Client().get(f'/r/{b.code}')
        self.assertEqual((res.status_code, res['Location']), (302, f'/b/rest/r/{b.token}/'))
        self.assertEqual(Client().get('/r/nope').status_code, 404)

    def test_access(self):
        b = self.book()
        self.get(f'/api/restaurants/{self.r.id}/messages', client=self.login(self.member('adm', Role.ADMIN)), expect=403)
        viewer = self.login(self.member('view', Role.VIEWER))
        self.assertEqual(len(self.get(f'/api/bookings/{b["id"]}/messages', client=viewer)['messages']), 1)
        self.op(b['id'], 'remind', client=viewer, expect=403)
        log = self.get(f'/api/restaurants/{self.r.id}/messages')['log']
        self.assertEqual((log[0]['statusName'], log[0]['by']), ('Пробный режим: не отправлено', 'Иван'))

    def test_sender_name(self):
        self.assertEqual(self.post(f'/api/restaurants/{self.r.id}', {'smsSender': 'Naberezhnaya'}, expect=400)['code'], 'invalid')
        self.assertEqual(self.post(f'/api/restaurants/{self.r.id}', {'smsSender': 'Naberezhn'})['restaurant']['smsSender'], 'Naberezhn')
        self.post(f'/api/restaurants/{self.r.id}', {'smsSender': 'Набережная'}, expect=400)

    def test_parts(self):
        self.assertEqual((sms.parts('a' * 160), sms.parts('a' * 161), sms.parts('я' * 70), sms.parts('я' * 71), sms.parts('я' * 134)), (1, 2, 1, 2, 2))

    def test_live_sending(self):
        calls = []

        def ok(message):
            calls.append(message.phone)
            return 'id-1'

        def broken(message):
            raise sms.ProviderError('Недостаточно средств')

        with override_settings(SMS_PROVIDER='fake'):
            sms.PROVIDERS['fake'] = ok
            try:
                self.book()
                self.assertEqual(Message.objects.get().status, 'queued')
                self.assertEqual(sms.send_queued(), 1)
                m = Message.objects.get()
                self.assertEqual((m.status, m.provider_id, calls), ('sent', 'id-1', ['+79001112233']))
                sms.PROVIDERS['fake'] = broken
                self.book(name='Борис', phone='+7 900 222-33-44', table=self.t[3].id)
                sms.send_queued()
                failed = Message.objects.get(status='failed')
                self.assertEqual(failed.error, 'Недостаточно средств')
            finally:
                sms.PROVIDERS.pop('fake', None)


class Consent(Base):
    def test_no_sms_without_consent(self):
        b = self.book(sms=False, date=TOMORROW.isoformat())
        self.op(b['id'], 'shift', delta=30)
        self.now = at(16, 30, TOMORROW)
        self.assertEqual((Message.objects.count(), sms.queue_reminders(self.r)), (0, 0))
        self.assertFalse(self.get(f'/api/restaurants/{self.r.id}/sync?from={TOMORROW}&to={TOMORROW}')['bookings'][0]['sms'])
        self.post(f'/api/restaurants/{self.r.id}/wait', {'name': 'Игорь', 'phone': '+7 900 333-22-11', 'date': TODAY.isoformat(), 'start': 1200, 'guests': 4, 'sms': False})
        self.assertEqual(Message.objects.count(), 0)

    def test_guest_consent_is_stored_with_the_request(self):
        Client().post('/api/public/rest/book', data=json.dumps({'name': 'Пётр', 'phone': '+7 900 555-44-33', 'guests': 2, 'date': TOMORROW.isoformat(),
                                                               'start': 19 * 60, 'consent': True}), content_type='application/json')
        event = Booking.objects.get().events.get()
        self.assertEqual(event.data['consent_text'], 'Согласен на обработку имени и телефона и на SMS об этой брони по политике ресторана')


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@override_settings(SMS_PROVIDER='smsru', SMS_API_KEY='test-key', SMS_TEST=True)
class SmsRu(Base):
    def send(self, answer):
        from unittest import mock
        seen = {}

        def urlopen(request, timeout):
            seen.update(dict(p.split('=', 1) for p in request.data.decode().split('&')))
            if isinstance(answer, Exception):
                raise answer
            return FakeResponse(answer)

        self.r.sms_sender = 'Naberezhn'
        self.r.save()
        with mock.patch('booking.sms.urllib.request.urlopen', urlopen):
            sms.send_queued()
        return seen

    def test_sent(self):
        self.book()
        seen = self.send({'status': 'OK', 'sms': {'79001112233': {'status': 'OK', 'sms_id': '000-1'}}, 'balance': 100})
        self.assertEqual((seen['to'], seen['from'], seen['test'], seen['api_id']), ('79001112233', 'Naberezhn', '1', 'test-key'))
        m = Message.objects.get()
        self.assertEqual((m.status, m.provider_id), ('sent', '000-1'))

    def test_refused(self):
        self.book()
        self.send({'status': 'OK', 'sms': {'79001112233': {'status': 'ERROR', 'status_text': 'Номер в стоп-листе'}}})
        self.assertEqual((Message.objects.get().status, Message.objects.get().error), ('failed', 'Номер в стоп-листе'))

    def test_no_connection_retries_then_gives_up(self):
        import urllib.error
        self.book()
        self.send(urllib.error.URLError('timeout'))
        self.assertEqual(Message.objects.get().status, 'queued')
        self.now = at(15, 31)
        self.send(urllib.error.URLError('timeout'))
        self.assertEqual(Message.objects.get().status, 'failed')
