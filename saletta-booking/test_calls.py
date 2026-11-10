import json
from datetime import timedelta

from django.test import Client

from booking.models import CallEvent, Guest, Membership, Restaurant, Role

from .base import Base


class IncomingCalls(Base):
    def enable(self):
        url = self.post(f'/api/restaurants/{self.r.id}/calls/setup', {'op': 'on'})['url']
        self.assertTrue(url.startswith('http://localhost:8765/call/'))
        return '/' + url.split('/', 3)[3]

    def recent(self, after=0, client=None, expect=200):
        return self.get(f'/api/restaurants/{self.r.id}/calls?after={after}', client=client, expect=expect)

    def test_off_by_default_and_setup_by_managers_only(self):
        self.assertFalse(self.get('/api/bootstrap')['restaurants'][0]['calls'])
        self.assertEqual(self.get(f'/api/restaurants/{self.r.id}/calls/setup')['url'], '')
        self.get(f'/api/restaurants/{self.r.id}/calls/setup', client=self.login(self.member('adm', Role.ADMIN)), expect=403)
        path = self.enable()
        self.assertTrue(self.get('/api/bootstrap')['restaurants'][0]['calls'])
        self.assertEqual(Client().get(path + '?phone=89001112233').status_code, 200)
        new_path = self.enable()                                      # новый адрес — старый перестаёт работать
        self.assertEqual(Client().get(path + '?phone=89001112233').status_code, 404)
        self.post(f'/api/restaurants/{self.r.id}/calls/setup', {'op': 'off'})
        self.assertEqual(Client().get(new_path + '?phone=89001112233').status_code, 404)
        self.assertEqual(Client().get('/call/short?phone=89001112233').status_code, 404)

    def test_known_guest_pops_up_with_history(self):
        path = self.enable()
        self.book(name='Анна', phone='+7 900 111-22-33')
        Guest.objects.filter(phone_key='9001112233').update(vip=True, note='Любит стол у окна')
        Client().get(path + '?phone=8 (900) 111-22-33')
        calls = self.recent()['calls']
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]['guest']['name'], calls[0]['guest']['vip'], calls[0]['guest']['note']), ('Анна', True, 'Любит стол у окна'))
        self.assertEqual(self.recent(after=calls[0]['id'])['calls'], [])
        # Хостес тоже видит, кто звонит
        self.assertEqual(len(self.recent(client=self.login(self.member('adm', Role.ADMIN)))['calls']), 1)

    def test_different_telephony_formats(self):
        path = self.enable()
        c = Client()
        self.assertEqual(c.post(path, {'CallerIDNum': '+79002223344'}).status_code, 200)
        self.assertEqual(c.post(path, data=json.dumps({'from': '79003334455'}), content_type='application/json').status_code, 200)
        self.assertEqual(c.get(path + '?caller=9004445566').status_code, 200)
        self.assertEqual(c.get(path + '?phone=12345').status_code, 400)
        self.assertEqual(c.get(path).status_code, 400)
        calls = self.recent()['calls']
        self.assertEqual({x['phone'] for x in calls}, {'+79002223344', '79003334455', '9004445566'})
        self.assertTrue(all(x['guest'] is None for x in calls))

    def test_old_calls_are_not_shown_and_flood_is_limited(self):
        path = self.enable()
        Client().get(path + '?phone=89001112233')
        self.now += timedelta(minutes=3)
        self.assertEqual(self.recent()['calls'], [])
        codes = [Client().get(path + '?phone=89001112233').status_code for _ in range(61)]
        self.assertEqual((codes.count(200), codes[-1]), (60, 429))

    def test_other_restaurant_staff_do_not_see_calls(self):
        self.enable()
        second = Restaurant.objects.create(org=self.org, name='Второй', slug='second')
        user = self.member('man', Role.MANAGER)
        m = Membership.objects.get(user=user)
        m.all_restaurants = False
        m.save()
        m.restaurants.add(second)
        self.recent(client=self.login(user), expect=404)

    def test_erasing_guest_removes_their_calls(self):
        path = self.enable()
        self.book(name='Анна', phone='+7 900 111-22-33')
        Client().get(path + '?phone=89001112233')
        guest = Guest.objects.get(phone_key='9001112233')
        self.post(f'/api/guests/{guest.id}', {'op': 'erase'})
        self.assertFalse(CallEvent.objects.exists())
