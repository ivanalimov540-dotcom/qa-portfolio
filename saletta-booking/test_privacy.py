from datetime import timedelta

from django.test import Client

from booking.models import Booking, Guest, Message, Role, WaitEntry

from .base import TODAY, Base


class GuestData(Base):
    def test_erase_guest_keeps_anonymous_bookings(self):
        b = self.book(note='Аллергия на орехи')
        other = self.book(name='Борис', phone='+7 900 222-33-44', table=self.t[3].id)
        self.post(f'/api/restaurants/{self.r.id}/wait', {'name': 'Анна', 'phone': '+7 900 111-22-33', 'date': TODAY.isoformat(), 'start': 1260, 'guests': 2})
        guest = Guest.objects.get(phone_key='9001112233')
        self.post(f'/api/guests/{guest.id}', {'op': 'erase'}, client=self.login(self.member('adm', Role.ADMIN)), expect=403)
        self.assertEqual(self.post(f'/api/guests/{guest.id}', {'op': 'erase'})['erased'], 1)
        gone = Booking.objects.get(pk=b['id'])
        self.assertEqual((gone.name, gone.phone, gone.note, gone.guest_id), ('Гость удалён', '', '', None))
        self.assertFalse(Guest.objects.filter(pk=guest.id).exists())
        self.assertFalse(Message.objects.filter(phone='+79001112233').exists())
        self.assertFalse(WaitEntry.objects.exists())
        self.assertEqual(Booking.objects.get(pk=other['id']).name, 'Борис')
        self.assertEqual(Message.objects.filter(phone='+79002223344').count(), 1)
        self.get(f'/api/guests/{guest.id}', expect=404)

    def test_history_of_a_booking(self):
        b = self.book()
        self.op(b['id'], 'shift', delta=30)
        self.op(b['id'], 'move', table=self.t[3].id)
        self.op(b['id'], 'status', status='cancelled', reason='Дубль брони')
        events = self.get(f'/api/bookings/{b["id"]}/messages')['events']
        self.assertEqual([e['text'] for e in events], ['Создал бронь', 'Сдвинул время: 19:00 → 19:30', 'Пересадил: стол 2 → стол 3', 'Отменил: дубль брони'])
        self.assertEqual(events[0]['who'], 'Иван')

    def test_privacy_page_uses_legal_details(self):
        page = Client().get('/b/rest/privacy/')
        self.assertContains(page, 'ресторан «Ресторан»')
        self.post(f'/api/restaurants/{self.r.id}', {'legalName': 'ООО «Набережная»', 'inn': '7701234567', 'privacyContact': 'privacy@example.ru'})
        page = Client().get('/b/rest/privacy/')
        self.assertContains(page, 'ООО «Набережная», ИНН 7701234567')
        self.assertContains(page, 'privacy@example.ru')
        self.post(f'/api/restaurants/{self.r.id}', {'inn': '12345'}, expect=400)
        self.assertEqual(Client().get('/b/nope/privacy/').status_code, 404)


class Export(Base):
    def test_guests_and_bookings_to_excel(self):
        self.book(name='=HYPERLINK("x")', note='Окно')
        res = self.c.get('/api/export/guests.csv')
        text = res.content.decode('utf-8')
        self.assertEqual((res.status_code, res['Content-Type']), (200, 'text/csv; charset=utf-8'))
        self.assertTrue(text.startswith('﻿Имя;Телефон'))
        self.assertIn('\'=HYPERLINK', text)
        res = self.c.get(f'/api/export/bookings.csv?from={TODAY}&to={TODAY}')
        self.assertIn('Подтверждена;по звонку', res.content.decode('utf-8'))
        self.assertEqual(self.c.get('/api/export/bookings.csv?from=x&to=y').status_code, 400)
        admin = self.login(self.member('adm', Role.ADMIN))
        self.assertEqual(admin.get('/api/export/guests.csv').status_code, 403)
        self.assertEqual(Client().get('/api/export/guests.csv').status_code, 401)
