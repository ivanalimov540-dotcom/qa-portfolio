import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase

from booking.api import new_organization
from booking.models import Hall, Membership, Restaurant, Role, Table
from booking.rules import min_guests_for

User = get_user_model()
TODAY = date(2026, 10, 11)


def at(hour, minute=0, day=TODAY):
    """Момент по московскому времени."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt_timezone(timedelta(hours=3)))


class Base(TestCase):
    """Сеть с одним рестораном: столы 1 (2 места), 2 и 3 (по 4), 4 (6 мест, от трёх гостей). Сейчас 15:00."""

    now = at(15)

    def setUp(self):
        cache.clear()
        patcher = mock.patch('django.utils.timezone.now', side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = User.objects.create_user('owner', password='pw-owner-12345', first_name='Иван')
        self.org = new_organization('Сеть', self.owner)
        self.r = Restaurant.objects.create(org=self.org, name='Ресторан', slug='rest', phone='+7 900 000-10-01', open_min=720, close_min=1380)
        self.hall = Hall.objects.create(restaurant=self.r, name='Зал')
        self.t = {n: Table.objects.create(restaurant=self.r, hall=self.hall, number=n, seats=s, min_guests=min_guests_for(s))
                  for n, s in ((1, 2), (2, 4), (3, 4), (4, 6))}
        self.c = self.login(self.owner)

    def member(self, login, role):
        user = User.objects.create_user(login, password='pw-' + login + '-12345', first_name=login)
        Membership.objects.create(user=user, org=self.org, role=role)
        return user

    def login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def post(self, url, data=None, client=None, expect=200):
        res = (client or self.c).post(url, data=json.dumps(data or {}), content_type='application/json')
        self.assertEqual(res.status_code, expect, res.content.decode())
        return res.json()

    def get(self, url, client=None, expect=200):
        res = (client or self.c).get(url)
        self.assertEqual(res.status_code, expect, res.content.decode())
        return res.json()

    def data(self, **over):
        d = {'name': 'Анна', 'phone': '+7 900 111-22-33', 'guests': 2, 'date': TODAY.isoformat(), 'start': 19 * 60, 'dur': 120,
             'table': self.t[2].id, 'extra': [], 'tags': [], 'note': ''}
        d.update(over)
        return d

    def book(self, **over):
        return self.post(f'/api/restaurants/{self.r.id}/bookings', self.data(**over))['bookings'][0]

    def op(self, booking_id, op, expect=200, client=None, **more):
        return self.post(f'/api/bookings/{booking_id}', {'op': op, **more}, client=client, expect=expect)
