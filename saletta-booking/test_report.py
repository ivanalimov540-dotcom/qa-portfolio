from datetime import timedelta

from booking.models import Booking, Guest, Role, WaitEntry

from .base import TODAY, Base

SAT = TODAY - timedelta(days=1)  # 10 октября 2026 — суббота


class Report(Base):
    def past(self, date, status, table=2, start=19 * 60, dur=120, guests=2, source='phone', guest=None, reason=''):
        return Booking.objects.create(restaurant=self.r, table=self.t[table], guest=guest, name=guest.name if guest else 'Гость',
                                      phone=guest.phone if guest else '', guests=guests, date=date, start_min=start, dur_min=dur,
                                      status=status, source=source, cancel_reason=reason)

    def report(self, period='w', client=None, expect=200, scope='all'):
        return self.get(f'/api/report?period={period}&scope={scope}', client=client, expect=expect)

    def test_week_counts_sources_and_delta(self):
        new = Guest.objects.create(org=self.org, name='Новая', phone='+7 900 1', phone_key='9000000001')
        old = Guest.objects.create(org=self.org, name='Старый', phone='+7 900 2', phone_key='9000000002', visits_before=5)
        self.past(SAT, 'done', guests=4, guest=new)
        self.past(SAT, 'done', table=3, guests=3, guest=old, source='site')
        self.past(SAT - timedelta(days=2), 'done', table=1, guests=2, source='walkin')
        self.past(SAT - timedelta(days=3), 'noshow', guests=5, guest=old)
        self.past(SAT - timedelta(days=4), 'cancelled', reason='Дубль брони')
        self.past(SAT - timedelta(days=5), 'cancelled', reason='Дубль брони')
        self.past(SAT - timedelta(days=8), 'done')                      # прошлая неделя
        self.past(TODAY, 'seated')                                      # сегодня в неделю не входит
        rep = self.report()
        m = rep['m']
        self.assertEqual((rep['from'], rep['to']), ('2026-10-04', '2026-10-10'))
        self.assertEqual((m['visits'], m['came'], m['walkin'], m['people'], m['noshow'], m['cancelled'], m['bookings']), (3, 2, 1, 9, 1, 2, 5))
        self.assertEqual((m['fresh'], rep['sources']), (1, {'phone': 4, 'site': 1, 'walkin': 1}))
        self.assertEqual(rep['tiles']['visits'], {'now': 3, 'was': 1})
        self.assertEqual(rep['tiles']['noshowRate']['now'], 33)
        self.assertEqual(rep['reasons'], [{'name': 'Дубль брони', 'v': 2}])
        self.assertEqual([g['name'] for g in rep['top']], ['Новая', 'Старый'])
        self.assertIn('на 200% больше', rep['say'][0])
        self.assertIsNone(rep['loss'])
        self.assertIn('средний чек', rep['answers']['loss'])

    def test_load_by_weekday_and_hour(self):
        for n in (1, 2, 3, 4):
            self.past(SAT, 'done', table=n, start=12 * 60, dur=11 * 60)
        rep = self.report()
        sat = rep['weekday'][5]
        self.assertEqual((sat['lab'], sat['v']), ('Сб', 100))
        self.assertEqual(rep['weekday'][0]['v'], 0)
        self.assertEqual({h['lab']: h['v'] for h in rep['hourly']}['19'], 14)   # один день из семи
        self.assertEqual(rep['tiles']['load']['now'], 14)
        self.assertIn('по субботам', ' '.join(rep['say']))

    def test_group_booking_counts_every_table(self):
        b = self.past(SAT, 'done', table=4, start=12 * 60, dur=11 * 60, guests=10)
        b.extra_tables.set([self.t[3]])
        self.assertEqual(self.report()['weekday'][5]['v'], 50)

    def test_losses_by_average_check(self):
        self.past(SAT, 'noshow', guests=4)
        res = self.post(f'/api/restaurants/{self.r.id}', {'avgCheck': 1500})['restaurant']
        self.assertEqual(res['avgCheck'], 1500)
        rep = self.report()
        self.assertEqual(rep['loss'], 6000)
        self.assertIn('6 000 ₽', ' '.join(rep['say']))

    def test_today_includes_expected_guests(self):
        self.past(TODAY, 'seated', guests=4)
        self.past(TODAY, 'confirmed', table=3, start=20 * 60)
        self.past(TODAY - timedelta(days=7), 'done')
        rep = self.report('d')
        self.assertEqual((rep['m']['visits'], rep['m']['upcoming'], rep['tiles']['visits']), (1, 1, {'now': 1, 'was': 1}))
        self.assertEqual(rep['weekday'], [])
        self.assertIn('Ещё ждём 1', rep['say'][0])

    def test_waitlist_left_without_table(self):
        WaitEntry.objects.create(restaurant=self.r, date=SAT, start_min=1200, guests=4, name='Игорь')
        self.past(SAT, 'done')
        self.assertEqual(self.report()['waits'], 1)

    def test_empty_period(self):
        rep = self.report('m')
        self.assertEqual(rep['say'], ['За последние 28 дней броней и посещений в системе нет.'])

    def test_access(self):
        self.report(client=self.login(self.member('adm', Role.ADMIN)), expect=403)
        self.report(client=self.login(self.member('view', Role.VIEWER)))
        self.report(scope=999999, expect=404)
        self.assertEqual(self.report(scope=self.r.id)['rows'], [])
