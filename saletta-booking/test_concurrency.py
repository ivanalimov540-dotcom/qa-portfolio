import threading
import unittest
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase

from booking import services
from booking.api import new_organization
from booking.models import Booking, Hall, Restaurant, Table
from booking.services import Problem


@unittest.skipUnless(connection.vendor == 'postgresql', 'гонку проверяем на PostgreSQL, как на сервере')
class SameTableAtOnce(TransactionTestCase):
    def test_only_one_of_simultaneous_bookings_wins(self):
        user = get_user_model().objects.create_user('owner', password='x' * 12)
        org = new_organization('Сеть', user)
        r = Restaurant.objects.create(org=org, name='Р', slug='r', open_min=720, close_min=1380)
        hall = Hall.objects.create(restaurant=r, name='Зал')
        table = Table.objects.create(restaurant=r, hall=hall, number=1, seats=4)
        day = (date.today() + timedelta(days=2)).isoformat()
        start = threading.Barrier(8)
        results = []

        def attempt(n):
            try:
                start.wait()
                services.create_booking(r, user, {'name': f'Гость {n}', 'phone': f'+7 900 000-11-{n:02d}', 'guests': 2,
                                                  'date': day, 'start': 1140, 'dur': 120, 'table': table.id})
                results.append('ok')
            except Problem:
                results.append('busy')
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((results.count('ok'), results.count('busy')), (1, 7))
        self.assertEqual(Booking.objects.count(), 1)
