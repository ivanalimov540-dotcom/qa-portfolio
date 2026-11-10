import json
from datetime import timedelta
from unittest import mock

from django.test import Client, override_settings

from booking import mailing, sms
from booking.models import AdsConsent, Booking, Campaign, ConsentLog, Guest, Membership, Message, Restaurant, Role

from .base import TODAY, Base, at

TOMORROW = TODAY + timedelta(days=1)


def site_booking(slug='rest', **over):
    d = {'name': 'Пётр', 'phone': '+7 900 555-44-33', 'guests': 2, 'date': TOMORROW.isoformat(), 'start': 19 * 60, 'consent': True}
    d.update(over)
    res = Client().post(f'/api/public/{slug}/book', data=json.dumps(d), content_type='application/json')
    assert res.status_code == 200, res.content.decode()
    return res.json()


class Consent(Base):
    def test_site_checkbox_counts_only_after_the_visit(self):
        info = Client().get('/api/public/rest/info').json()['restaurant']
        self.assertEqual(info['adsText'], mailing.ads_text(self.r))
        self.assertIn('«Ресторан»', info['adsText'])
        res = site_booking(ads=True)
        b = Booking.objects.get(token=res['receipt']['token'])
        g = b.guest
        self.assertTrue(b.ads_pending)
        self.assertFalse(AdsConsent.objects.exists())          # номер никто не проверял — пока не действует
        self.op(b.id, 'status', status='confirmed')
        self.now = at(19, 10, TOMORROW)
        self.op(b.id, 'status', status='seated')
        self.op(b.id, 'status', status='done')
        consent = AdsConsent.objects.get(guest=g)
        self.assertEqual(consent.restaurant, self.r)
        self.assertIn('подтверждено визитом', consent.how)
        log = ConsentLog.objects.get(guest=g)
        self.assertEqual((log.ok, log.text, log.booking_id), (True, mailing.ads_text(self.r), b.id))

    def test_stranger_cannot_subscribe_someone_elses_number(self):
        res = site_booking(ads=True)
        Client().post(f'/api/public/receipt/{res["receipt"]["token"]}', data='{"op": "cancel"}', content_type='application/json')
        self.assertFalse(AdsConsent.objects.exists())

    def test_staff_records_consent_for_this_restaurant_and_withdraws_all(self):
        b = self.book(ads=True)
        g = Guest.objects.get(pk=b['gid'])
        self.assertEqual(list(AdsConsent.objects.values_list('restaurant_id', 'how')), [(self.r.id, 'Записал сотрудник Иван')])
        second = Restaurant.objects.create(org=self.org, name='Второй', slug='second')
        card = self.post(f'/api/guests/{g.id}', {'ads': second.id})
        self.assertEqual([a['restName'] for a in card['ads']], ['Ресторан', 'Второй'])
        card = self.post(f'/api/guests/{g.id}', {'ads': 'withdraw'})
        self.assertEqual(card['ads'], [])
        self.assertEqual(card['guest']['ads'], [])
        self.assertEqual(sorted(ConsentLog.objects.values_list('ok', flat=True)), [False, False, True, True])  # история не стирается
        self.book(table=self.t[3].id, start=21 * 60)            # бронь без галочки согласие не возвращает
        self.assertFalse(AdsConsent.objects.exists())


class Mailings(Base):
    def setUp(self):
        super().setUp()
        self.anna = self.subscribed('Анна Смирнова', '9000000001', visits_before=6, last_visit_before=TODAY - timedelta(days=5))
        self.boris = self.subscribed('Борис', '9000000002', vip=True)
        Guest.objects.create(org=self.org, name='Без согласия', phone='+7 900 000-00-03', phone_key='9000000003')
        self.subscribed('Чёрный', '9000000004', blacklisted=True)
        self.subscribed('Городской', '4950000005')

    def subscribed(self, name, key, restaurant=None, **extra):
        g = Guest.objects.create(org=self.org, name=name, phone=f'+7 {key[:3]} {key[3:6]}-{key[6:8]}-{key[8:]}', phone_key=key, **extra)
        mailing.grant(g, restaurant or self.r, 'Записал сотрудник')
        return g

    def body(self, **over):
        d = {'op': 'preview', 'from': self.r.id, 'text': '{имя}, в пятницу живая музыка. Бронь: {ссылка}'}
        d.update(over)
        return d

    def test_preview_counts_only_guests_who_agreed_to_this_restaurant(self):
        pv = self.post('/api/mailings', self.body())
        self.assertEqual((pv['count'], pv['recent'], pv['mode']), (2, 0, 'test'))
        self.assertTrue(pv['sample'].startswith('Анна, в пятницу живая музыка. Бронь: http://localhost:8765/b/rest/\nОтписаться: http://localhost:8765/u/'))
        self.assertEqual(self.post('/api/mailings', self.body(vip=True))['count'], 1)
        self.assertEqual(self.post('/api/mailings', self.body(segs=['regular']))['count'], 1)
        other = Restaurant.objects.create(org=self.org, name='Другое ООО', slug='other')
        self.assertEqual(self.post('/api/mailings', self.body(**{'from': other.id}))['count'], 0)  # согласие не переходит
        self.assertEqual(self.get('/api/mailings')['subscribers'], {str(self.r.id): 3})
        self.assertIn('{скидка}', self.post('/api/mailings', self.body(text='Ваша {скидка}'), expect=400)['error'])
        self.post('/api/mailings', self.body(text=''), expect=400)

    def test_preview_counts_parts_for_every_guest(self):
        long = 'Длинноеимя' * 4
        self.subscribed(long, '9000000006')
        text = 'x' * 70
        pv = self.post('/api/mailings', self.body(text=text))
        guests = Guest.objects.filter(phone_key__in=['9000000001', '9000000002', '9000000006'])
        expected = sum(sms.parts(mailing.render(text, self.r, g.name, mailing.unsub_link(g))) for g in guests)
        self.assertEqual(pv['total'], expected)

    def test_send_logs_messages_and_respects_weekly_gap(self):
        res = self.post('/api/mailings', self.body(op='send'))
        self.assertEqual(res['campaign']['recipients'], 2)
        msgs = Message.objects.filter(kind='mailing').order_by('phone')
        self.assertEqual([m.phone for m in msgs], ['+79000000001', '+79000000002'])
        self.assertTrue(all(m.status == 'test' and m.campaign_id == res['campaign']['id'] and m.guest_id for m in msgs))
        self.assertIn(mailing.unsub_link(self.anna), msgs[0].text)
        self.assertIn('Некому отправлять', self.post('/api/mailings', self.body(op='send'), expect=400)['error'])
        pv = self.post('/api/mailings', self.body())
        self.assertEqual((pv['count'], pv['recent']), (0, 2))
        self.now += timedelta(days=8)
        self.assertEqual(self.post('/api/mailings', self.body())['count'], 2)

    @override_settings(SMS_PROVIDER='smsru')
    def test_night_mailing_waits_and_unsubscribe_cancels_it(self):
        self.now = at(22, 30)
        self.post('/api/mailings', self.body(op='send'))
        self.assertEqual(Campaign.objects.get().send_at, at(9, 0, TOMORROW))
        Client().post(f'/u/{mailing.unsub_code(self.anna)}')          # отписалась ночью, пока рассылка ждала утра
        with mock.patch.dict(sms.PROVIDERS, {'smsru': lambda m: 'id-1'}):
            self.assertEqual(sms.send_queued(), 0)
            self.now = at(9, 1, TOMORROW)
            self.assertEqual(sms.send_queued(), 1)
        status = dict(Message.objects.values_list('phone', 'status'))
        self.assertEqual(status, {'+79000000001': 'cancelled', '+79000000002': 'sent'})

    @override_settings(SMS_PROVIDER='smsru')
    def test_long_mailing_does_not_spill_into_the_night(self):
        self.now = at(20, 59)
        self.post('/api/mailings', self.body(op='send'))
        self.now = at(21, 5)
        with mock.patch.dict(sms.PROVIDERS, {'smsru': lambda m: 'id-1'}):
            self.assertEqual(sms.send_queued(), 0)
        self.assertEqual(set(Message.objects.values_list('send_after', flat=True)), {at(9, 0, TOMORROW)})

    @override_settings(SMS_PROVIDER='smsru')
    def test_temporary_trouble_in_the_morning_does_not_kill_the_campaign(self):
        self.now = at(22, 30)
        self.post('/api/mailings', self.body(op='send'))
        self.now = at(9, 1, TOMORROW)
        with mock.patch.dict(sms.PROVIDERS, {'smsru': mock.Mock(side_effect=sms.TemporaryError('нет связи'))}):
            sms.send_queued()
        self.assertEqual(set(Message.objects.values_list('status', flat=True)), {'queued'})

    def test_unsubscribe_link(self):
        code = mailing.unsub_code(self.anna)
        self.assertContains(Client().get(f'/u/{code}'), 'Отписаться от новостей?')
        self.assertContains(Client().post(f'/u/{code}'), 'Вы отписались')
        self.assertFalse(AdsConsent.objects.filter(guest=self.anna).exists())
        self.assertEqual(ConsentLog.objects.filter(guest=self.anna, ok=False).get().how, 'Отписался по ссылке из сообщения')
        self.assertEqual(Client().get(f'/u/{self.anna.id}-aaaaaaaa').status_code, 404)
        self.assertEqual(Client().get('/u/nonsense').status_code, 404)
        self.assertEqual(Client().get(f'/u/{self.anna.id}-абвгдежз').status_code, 404)
        self.assertEqual(self.post('/api/mailings', self.body())['count'], 1)

    def test_only_managers_with_whole_network(self):
        self.post('/api/mailings', self.body(), client=self.login(self.member('adm', Role.ADMIN)), expect=403)
        manager = self.member('man', Role.MANAGER)
        m = Membership.objects.get(user=manager)
        m.all_restaurants = False
        m.save()
        m.restaurants.add(self.r)
        self.post('/api/mailings', self.body(), client=self.login(manager), expect=403)

    def test_privacy_mentions_news(self):
        self.assertContains(Client().get('/b/rest/privacy/'), 'Новости и предложения')
