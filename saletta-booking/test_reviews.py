from datetime import timedelta

from django.test import Client

from booking import sms
from booking.models import Booking, BookingEvent, Guest, Message, Review, Role

from .base import TODAY, Base, at


class Reviews(Base):
    def visit(self, start=12 * 60, day=TODAY, phone='+7 900 111-22-33', table=None):
        """Гость пришёл и ушёл: бронь закрыта."""
        b = self.book(start=start, date=day.isoformat(), phone=phone, table=(table or self.t[2]).id)
        self.op(b['id'], 'status', status='seated')
        self.op(b['id'], 'status', status='done')
        return Booking.objects.get(pk=b['id'])

    def receipt(self, b, method='get', **body):
        c = Client()
        url = f'/api/public/receipt/{b.token}'
        res = c.get(url) if method == 'get' else c.post(url, data=__import__('json').dumps(body), content_type='application/json')
        return res.status_code, res.json()

    def test_request_goes_after_the_visit_and_not_at_night(self):
        b = self.visit()                                   # визит 12:00–14:00, закрыли в 15:00
        self.now = at(17, 59)
        self.assertEqual(sms.queue_reviews(self.r), 0)
        self.now = at(18)
        self.assertEqual(sms.queue_reviews(self.r), 1)
        m = Message.objects.get(kind='review')
        self.assertIn(f'/r/{b.code}', m.text)
        self.assertEqual(sms.queue_reviews(self.r), 0)     # второй раз не просим

        late = self.visit(start=13 * 60, table=self.t[3], phone='+7 900 222-33-44')
        self.now = at(21, 30)
        self.assertEqual(sms.queue_reviews(self.r), 0)     # тихие часы
        self.now = at(9, 0, TODAY + timedelta(days=1))
        self.assertEqual(sms.queue_reviews(self.r), 1)
        self.assertTrue(Booking.objects.get(pk=late.pk).review_asked)

    def test_regular_guest_is_asked_once_a_month_and_old_visits_are_skipped(self):
        self.visit()
        self.now = at(18)
        sms.queue_reviews(self.r)
        self.now = at(15, 0, TODAY + timedelta(days=2))
        self.visit(start=12 * 60, day=TODAY + timedelta(days=2))
        self.now = at(19, 0, TODAY + timedelta(days=2))
        self.assertEqual(sms.queue_reviews(self.r), 0)
        self.assertEqual(Message.objects.filter(kind='review').count(), 1)
        # Визит, о котором вспомнили через неделю, не оцениваем
        self.now = at(15)
        old = self.visit(start=12 * 60, phone='+7 900 555-66-77', table=self.t[3])
        self.now = at(12, 0, TODAY + timedelta(days=8))
        self.assertEqual(sms.queue_reviews(self.r), 0)
        self.assertFalse(Message.objects.filter(booking=old, kind='review').exists())

    def test_no_request_without_consent_or_when_turned_off(self):
        b = self.book(start=12 * 60, sms=False)
        self.op(b['id'], 'status', status='seated')
        self.op(b['id'], 'status', status='done')
        self.now = at(18)
        self.assertEqual(sms.queue_reviews(self.r), 0)
        self.post(f'/api/restaurants/{self.r.id}/templates', {'kind': 'review', 'enabled': False})
        self.now = at(15)
        self.visit(table=self.t[3], phone='+7 900 222-33-44')
        self.now = at(18)
        self.assertEqual(sms.queue_reviews(self.r), 0)

    def test_guest_rates_the_visit(self):
        self.post(f'/api/restaurants/{self.r.id}', {'yandexUrl': 'https://yandex.ru/maps/org/restoran/123/', 'gisUrl': ''})
        b = self.visit()
        code, res = self.receipt(b)
        self.assertEqual(res['receipt']['review'], {'open': True, 'rating': None, 'links': []})
        code, res = self.receipt(b, 'post', op='review', rating=2, comment='Долго ждали горячее')
        self.assertEqual(code, 200)
        self.assertEqual(res['receipt']['review'], {'open': False, 'rating': 2,
                                                    'links': [{'name': 'на Яндекс Картах', 'url': 'https://yandex.ru/maps/org/restoran/123/'}]})
        code, res = self.receipt(b, 'post', op='review', rating=5)
        self.assertEqual(code, 400)
        self.assertEqual(Review.objects.get().comment, 'Долго ждали горячее')
        self.assertIn('оценил визит: 2 из 5', [e['text'] for e in self.get(f'/api/bookings/{b.id}/messages')['events']][-1])
        # Живую бронь оценить нельзя
        live = Booking.objects.get(pk=self.book(start=20 * 60, table=self.t[3].id, phone='+7 900 222-33-44')['id'])
        code, res = self.receipt(live, 'post', op='review', rating=5)
        self.assertEqual(code, 400)

    def test_cabinet_list_and_handling(self):
        b = self.visit()
        self.receipt(b, 'post', op='review', rating=1, comment='Грубый официант')
        good = self.visit(table=self.t[3], phone='+7 900 222-33-44')
        self.receipt(good, 'post', op='review', rating=5)
        res = self.get('/api/reviews?f=open')
        self.assertEqual([x['rating'] for x in res['reviews']], [1])
        self.assertEqual((res['stats']['count'], res['stats']['avg'], res['stats']['open']), (2, 3.0, 1))
        self.assertEqual(self.get('/api/bootstrap')['reviewsOpen'], 1)
        self.get('/api/reviews', client=self.login(self.member('adm', Role.ADMIN)), expect=403)
        viewer = self.login(self.member('obs', Role.VIEWER))
        self.assertEqual(len(self.get('/api/reviews', client=viewer)['reviews']), 2)
        rid = res['reviews'][0]['id']
        self.post(f'/api/reviews/{rid}', {'op': 'handled'}, client=viewer, expect=403)
        done = self.post(f'/api/reviews/{rid}', {'op': 'handled'})
        self.assertEqual((done['reviewsOpen'], done['review']['handledBy']), (0, 'Иван'))
        self.assertTrue(BookingEvent.objects.filter(booking=b, action='review_handled').exists())
        history = self.get(f'/api/guests/{b.guest_id}')['history']
        self.assertEqual(history[0]['rating'], 1)

    def test_map_links_are_checked(self):
        url = f'/api/restaurants/{self.r.id}'
        self.post(url, {'yandexUrl': 'http://yandex.ru/maps/org/1'}, expect=400)
        self.post(url, {'yandexUrl': 'https://evil.example/yandex.ru'}, expect=400)
        self.post(url, {'gisUrl': 'https://yandex.ru/maps/org/1'}, expect=400)
        r = self.post(url, {'gisUrl': 'https://2gis.ru/moscow/firm/123'})['restaurant']
        self.assertEqual(r['gisUrl'], 'https://2gis.ru/moscow/firm/123')

    def test_erasing_guest_removes_review_text(self):
        b = self.visit()
        self.receipt(b, 'post', op='review', rating=4, comment='Анна, спасибо за торт')
        self.post(f'/api/guests/{b.guest_id}', {'op': 'erase'})
        r = Review.objects.get()
        self.assertEqual((r.comment, r.guest_id, r.rating), ('', None, 4))
        self.assertFalse(Guest.objects.exists())

    def test_report_mentions_ratings(self):
        b = self.visit()
        self.receipt(b, 'post', op='review', rating=5)
        say = ' '.join(self.get('/api/report?period=d&scope=all')['say'])
        self.assertIn('оценили визиты в среднем на 5 из 5 (1 оценка)', say)


class NextDayReview(Base):
    def test_next_day_means_noon_not_the_day_after(self):
        self.r.review_hours = 24
        self.r.save()
        self.now = at(19)
        b = self.book(start=19 * 60, table=self.t[2].id)
        self.op(b['id'], 'status', status='seated')
        self.now = at(21, 35)
        self.op(b['id'], 'status', status='done')
        self.now = at(11, 59, TODAY + timedelta(days=1))
        self.assertEqual(sms.queue_reviews(self.r), 0)
        self.now = at(12, 0, TODAY + timedelta(days=1))
        self.assertEqual(sms.queue_reviews(self.r), 1)

    def test_tricky_map_links_are_refused(self):
        url = f'/api/restaurants/{self.r.id}'
        for bad in ('https://evil.com\\@yandex.ru/maps', 'https://user@yandex.ru/maps', 'https://yandex.ru:8443/maps', 'https://yandex.ru /maps'):
            self.post(url, {'yandexUrl': bad}, expect=400)
