import json
from datetime import timedelta
from unittest import mock

from django.test import Client, override_settings

from booking import sms, telegram
from booking.models import Booking, Guest, Membership, Message, Role

from .base import TODAY, Base

TG = {'TG_TOKEN': '123:abc', 'TG_BOT': 'demo_bot', 'TG_SECRET': 's3cret'}
TOMORROW = TODAY + timedelta(days=1)


def hook(text, chat=555, kind='private', secret='s3cret', header='s3cret'):
    update = {'update_id': 1, 'message': {'message_id': 1, 'chat': {'id': chat, 'type': kind}, 'text': text}}
    return Client().post(f'/tg/{secret}', data=json.dumps(update), content_type='application/json',
                         HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=header)


@override_settings(**TG)
class GuestTelegram(Base):
    def booking(self):
        b = self.book(date=TOMORROW.isoformat())
        return Booking.objects.get(pk=b['id'])

    def test_receipt_offers_telegram_and_start_links_the_guest(self):
        b = self.booking()
        receipt = Client().get(f'/api/public/receipt/{b.token}').json()['receipt']
        self.assertEqual(receipt['tg'], {'on': False, 'link': f'https://t.me/demo_bot?start=b{b.code}'})
        res = hook(f'/start b{b.code}')
        self.assertEqual(res.json()['method'], 'sendMessage')
        self.assertIn('«Ресторан»', res.json()['text'])
        self.assertEqual(Guest.objects.get(pk=b.guest_id).tg_chat_id, 555)
        self.assertTrue(Client().get(f'/api/public/receipt/{b.token}').json()['receipt']['tg']['on'])
        self.assertIn('Не нашли бронь', hook('/start bWRONGCODE1').json()['text'])
        self.assertIn('Отключили', hook('/stop').json()['text'])
        self.assertIsNone(Guest.objects.get(pk=b.guest_id).tg_chat_id)

    def test_webhook_is_protected(self):
        self.assertEqual(hook('/start', secret='wrong').status_code, 404)
        self.assertEqual(hook('/start', header='wrong').status_code, 404)
        self.assertEqual(hook('/start', kind='group').status_code, 200)       # в группах молчим
        self.assertEqual(Client().get('/tg/s3cret').status_code, 404)
        with override_settings(TG_SECRET=''):
            self.assertEqual(hook('/start', secret='', header='').status_code, 404)

    def test_messages_go_to_telegram_without_sms_contract(self):
        b = self.booking()
        hook(f'/start b{b.code}')
        self.op(b.id, 'shift', delta=30)
        m = Message.objects.get(kind='changed')
        self.assertEqual((m.channel, m.chat_id, m.status, m.parts), ('tg', 555, 'queued', 1))
        sms_only = Message.objects.get(kind='confirmed')            # подтверждение ушло до подключения Telegram — по SMS
        self.assertEqual((sms_only.channel, sms_only.status), ('sms', 'test'))
        with mock.patch.object(telegram, 'send', return_value='77') as send:
            self.assertEqual(sms.send_queued(), 1)
        send.assert_called_once_with(555, m.text)
        m.refresh_from_db()
        self.assertEqual((m.status, m.provider_id), ('sent', '77'))

    def test_blocked_bot_falls_back_to_sms(self):
        b = self.booking()
        hook(f'/start b{b.code}')
        self.op(b.id, 'shift', delta=30)
        with mock.patch.object(telegram, 'send', side_effect=telegram.SendError('Forbidden: bot was blocked by the user', blocked=True)):
            sms.send_queued()
        self.assertIsNone(Guest.objects.get(pk=b.guest_id).tg_chat_id)
        copies = Message.objects.filter(kind='changed').order_by('id')
        self.assertEqual([(x.channel, x.status) for x in copies], [('tg', 'failed'), ('sms', 'test')])

    def test_network_trouble_keeps_message_queued(self):
        b = self.booking()
        hook(f'/start b{b.code}')
        self.op(b.id, 'shift', delta=30)
        with mock.patch.object(telegram, 'send', side_effect=telegram.Unavailable('timeout')):
            self.assertEqual(sms.send_queued(), 0)
        self.assertEqual(Message.objects.get(kind='changed').status, 'queued')

    def test_without_bot_nothing_changes(self):
        with override_settings(TG_TOKEN='', TG_BOT=''):
            b = self.booking()
            self.assertIsNone(Client().get(f'/api/public/receipt/{b.token}').json()['receipt']['tg'])
            self.assertIsNone(self.get('/api/bootstrap')['tg'])


@override_settings(**TG)
class StaffTelegram(Base):
    def link(self, user, chat):
        m = Membership.objects.get(user=user)
        return m, hook('/start ' + telegram.staff_payload(m), chat=chat)

    def test_staff_link_and_alerts(self):
        boot = self.get('/api/bootstrap')['tg']
        self.assertEqual((boot['on'], boot['bot']), (False, 'demo_bot'))
        owner, res = self.link(self.owner, 901)
        self.assertIn('Готово, Иван', res.json()['text'])
        self.assertIn('не подошла', hook(f'/start s{owner.id}-AAAAAAAAAA', chat=999).json()['text'])
        admin, _ = self.link(self.member('hostess', Role.ADMIN), 902)
        viewer, _ = self.link(self.member('watcher', Role.VIEWER), 903)
        other_rest = self.member('elsewhere', Role.MANAGER)
        m, _ = self.link(other_rest, 904)
        m.all_restaurants = False
        m.save()
        Client().post('/api/public/rest/book', data=json.dumps({'name': 'Пётр', 'phone': '+7 900 555-44-33', 'guests': 2,
                      'date': TOMORROW.isoformat(), 'start': 19 * 60, 'consent': True}), content_type='application/json')
        alerts = Message.objects.filter(kind='staff').order_by('chat_id')
        self.assertEqual([a.chat_id for a in alerts], [901, 902])
        self.assertIn('Новая заявка с сайта, Ресторан: Пётр, 2 чел.', alerts[0].text)
        self.assertEqual(self.get(f'/api/restaurants/{self.r.id}/messages')['log'], [])   # в журнал гостевых сообщений не попадают
        self.assertFalse(self.post('/api/telegram/off', {})['tg']['on'])
        self.assertIsNone(Membership.objects.get(pk=owner.pk).tg_chat_id)

    def test_bad_review_alerts_only_managers(self):
        self.link(self.owner, 901)
        self.link(self.member('hostess', Role.ADMIN), 902)
        b = self.book(start=12 * 60)
        self.op(b['id'], 'status', status='seated')
        self.op(b['id'], 'status', status='done')
        token = Booking.objects.get(pk=b['id']).token
        Client().post(f'/api/public/receipt/{token}', data=json.dumps({'op': 'review', 'rating': 2, 'comment': 'Шумно'}), content_type='application/json')
        alert = Message.objects.get(kind='staff')
        self.assertEqual(alert.chat_id, 901)
        self.assertIn('Плохая оценка, Ресторан: 2 из 5', alert.text)


@override_settings(**TG)
class SetupCommand(Base):
    def test_requires_https_site(self):
        from django.core.management import CommandError, call_command
        with override_settings(SITE_URL='http://localhost:8765'):
            with self.assertRaises(CommandError):
                call_command('tg_setup')
        with override_settings(SITE_URL='https://bron.example.ru'), mock.patch.object(telegram, 'call', return_value={'username': 'demo_bot'}) as call:
            call_command('tg_setup')
        self.assertEqual(call.call_args_list[0].args[1]['url'], 'https://bron.example.ru/tg/s3cret')
