import json
from unittest import mock
from urllib.error import URLError

from django.core.management import call_command
from django.test import TestCase, override_settings

from booking import leads
from booking.models import Lead

URL = '/api/public/lead'
ORIGIN = 'https://saletta.ru'
GOOD = {'name': 'Антон', 'phone': '+7 928 000-00-01', 'telegram': '@anton_rest', 'consent': 'Согласен на обработку'}


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class LeadFormTests(TestCase):
    def post(self, **changes):
        return self.client.post(URL, json.dumps({**GOOD, **changes}), content_type='application/json', headers={'Origin': ORIGIN})

    def test_saved_without_touching_telegram(self):
        with mock.patch('urllib.request.urlopen') as net:
            self.assertEqual(self.post().status_code, 200)
        net.assert_not_called()
        lead = Lead.objects.get()
        self.assertEqual((lead.name, lead.telegram), ('Антон', 'anton_rest'))
        self.assertIsNone(lead.sent_at)

    def test_only_our_site_gets_cors(self):
        self.assertEqual(self.post()['Access-Control-Allow-Origin'], ORIGIN)
        other = self.client.post(URL, json.dumps(GOOD), content_type='application/json', headers={'Origin': 'https://evil.example'})
        self.assertNotIn('Access-Control-Allow-Origin', other)

    def test_preflight(self):
        response = self.client.options(URL, headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 204)
        self.assertIn('POST', response['Access-Control-Allow-Methods'])

    def test_validation(self):
        self.assertEqual(self.post(name='').json()['field'], 'name')
        self.assertEqual(self.post(phone='123').json()['field'], 'phone')
        self.assertEqual(self.post(telegram='ник с пробелом').json()['field'], 'telegram')
        self.assertEqual(self.post(consent='').json()['field'], 'consent')
        self.assertFalse(Lead.objects.exists())

    def test_telegram_is_optional_and_links_are_understood(self):
        self.post(telegram='')
        self.assertEqual(Lead.objects.get().telegram, '')
        self.assertEqual(leads.clean_telegram('https://t.me/anton_rest/'), 'anton_rest')

    def test_honeypot_is_quietly_ignored(self):
        self.assertEqual(self.post(website='spam').status_code, 200)
        self.assertFalse(Lead.objects.exists())

    def test_ip_limit(self):
        for n in range(5):
            self.assertEqual(self.post(phone=f'+7 928 000-00-1{n}').status_code, 200)
        self.assertEqual(self.post(phone='+7 928 000-00-20').status_code, 429)
        self.assertEqual(Lead.objects.count(), 5)

    def test_same_phone_is_not_repeated_forever(self):
        for _ in range(4):
            self.post()
        self.assertEqual(Lead.objects.count(), 3)


@override_settings(LEAD_TG_TOKEN='t', LEAD_TG_CHATS=['1', '2'], LEAD_TG_API='https://relay.example/')
class LeadSendTests(TestCase):
    def setUp(self):
        self.lead = Lead.objects.create(name='Антон', phone='+79280000001', telegram='anton_rest', consent='да')

    def test_sent_once_to_every_chat_through_configured_address(self):
        with mock.patch('urllib.request.urlopen', return_value=FakeResponse({'ok': True})) as net:
            self.assertEqual(leads.send_pending(), 1)
            self.assertEqual(leads.send_pending(), 0)
        self.assertEqual(net.call_count, 2)
        request = net.call_args_list[0].args[0]
        self.assertEqual(request.full_url, 'https://relay.example/bott/sendMessage')
        payload = json.loads(request.data)
        self.assertIn('Телефон: +79280000001', payload['text'])
        self.assertIn('Telegram: @anton_rest', payload['text'])
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.sent_at)

    def test_kept_when_telegram_is_down_and_token_not_logged(self):
        with mock.patch('urllib.request.urlopen', side_effect=URLError('https://relay.example/bott/sendMessage')):
            self.assertEqual(leads.send_pending(), 0)
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.sent_at)
        self.assertEqual(self.lead.error, 'URLError')
        with mock.patch('urllib.request.urlopen', return_value=FakeResponse({'ok': True})):
            call_command('sweep')
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.sent_at)

    @override_settings(LEAD_TG_TOKEN='')
    def test_nothing_happens_until_configured(self):
        with mock.patch('urllib.request.urlopen') as net:
            self.assertEqual(leads.send_pending(), 0)
        net.assert_not_called()
