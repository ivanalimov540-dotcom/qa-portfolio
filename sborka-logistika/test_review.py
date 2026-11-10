"""Ошибки, найденные при проверке перед запуском (21.10.2026). Каждый тест — одна найденная ошибка."""

import io
import json
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from accounts import roles
from sklad import importer as I
from sklad import kpd as K
from sklad import services as S
from sklad import tasks as T
from sklad.models import Address, Assembler, Board, Car, Counterparty, KpdCell, Option, Placement, Supplier, TaskDay

User = get_user_model()
TODAY = date(2026, 10, 21)


class Base(TestCase):
    def setUp(self):
        for target in ('sklad.services.today', 'sklad.tasks.today'):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.c829 = Car.objects.create(number='829', driver='Джафаров С.М.')
        self.c579 = Car.objects.create(number='579', driver='Баташов А.И.', sort=1)
        self.c066 = Car.objects.create(number='066', driver='Мишин В.Е.', sort=2)
        self.firm = Counterparty.objects.create(name='ООО «Союз»', car=self.c829)
        self.addr = Address.objects.create(counterparty=self.firm, text='ул. Закруткина, 42')
        self.ivan = Assembler.objects.create(name='Иван')
        self.director = User.objects.create_user('d', password='x' * 10, role=roles.DIRECTOR)
        self.assistant = User.objects.create_user('a', password='x' * 10, role=roles.ASSISTANT)
        self.manager = User.objects.create_user('m', password='x' * 10, role=roles.MANAGER)
        self.sklad = User.objects.create_user('s', password='x' * 10, role=roles.WAREHOUSE)

    def post(self, user, url, body=None):
        self.client.force_login(user)
        return self.client.post(url, json.dumps(body), content_type='application/json')

    def get(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)


class DirectorAccount(Base):
    def test_assistant_cannot_make_or_touch_director(self):
        self.assertEqual(self.post(self.assistant, '/api/users/new',
                                   {'name': 'Новый', 'username': 'boss2', 'role': 'director'}).status_code, 403)
        self.assertEqual(self.post(self.assistant, f'/api/users/{self.director.id}/password', {}).status_code, 403)
        self.assertEqual(self.post(self.assistant, f'/api/users/{self.director.id}', {'active': False}).status_code, 403)
        self.assertEqual(self.post(self.assistant, f'/api/users/{self.manager.id}', {'role': 'director'}).status_code, 403)
        self.director.refresh_from_db()
        self.assertTrue(self.director.is_active)
        self.assertFalse(User.objects.filter(username='boss2').exists())

    def test_assistant_still_manages_other_staff(self):
        res = self.post(self.assistant, '/api/users/new', {'name': 'Ольга', 'username': 'olga.k', 'role': 'manager'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.post(self.assistant, f'/api/users/{self.manager.id}/password', {}).status_code, 200)

    def test_director_can_add_second_director(self):
        res = self.post(self.director, '/api/users/new', {'name': 'Зам', 'username': 'zam', 'role': 'director'})
        self.assertEqual(res.status_code, 200)

    def test_login_must_be_simple_latin(self):
        for bad in ('ив ан', '<script>', 'a' * 41, 'ив'):
            res = self.post(self.director, '/api/users/new', {'name': 'X', 'username': bad, 'role': 'manager'})
            self.assertEqual(res.status_code, 400, bad)


class Dates(Base):
    def test_far_future_sheet_refused(self):
        for d in ('9999-12-31', '2027-10-22'):
            res = self.post(self.manager, '/api/sheets', {'date': d})
            self.assertEqual(res.status_code, 400, d)
        self.assertEqual(self.get(self.manager, '/api/bootstrap').status_code, 200)
        self.assertEqual(self.post(self.manager, '/api/sheets', {'date': '2026-12-20'}).status_code, 200)

    def test_wrong_date_can_be_fixed(self):
        S.create_sheet(self.manager, date(2026, 10, 21))
        wrong = S.create_sheet(self.manager, date(2026, 10, 28))
        res = self.post(self.manager, f'/api/sheets/{wrong.id}/date', {'date': '2026-10-22'})
        self.assertEqual(res.status_code, 200)
        wrong.refresh_from_db()
        self.assertEqual(wrong.date, date(2026, 10, 22))
        # не раньше последней таблицы в архиве и не на занятую дату
        self.assertEqual(self.post(self.manager, f'/api/sheets/{wrong.id}/date', {'date': '2026-10-21'}).status_code, 400)
        self.assertEqual(self.post(self.sklad, f'/api/sheets/{wrong.id}/date', {'date': '2026-10-23'}).status_code, 403)

    def test_archived_sheet_date_not_changed(self):
        old = S.create_sheet(self.manager, date(2026, 10, 21))
        S.create_sheet(self.manager, date(2026, 10, 22))
        self.assertEqual(self.post(self.manager, f'/api/sheets/{old.id}/date', {'date': '2026-10-20'}).status_code, 400)

    def test_task_day_far_future_refused_and_fixable(self):
        self.assertEqual(self.post(self.manager, '/api/tasks/new', {'date': '9999-12-31'}).status_code, 400)
        self.assertEqual(self.get(self.manager, '/api/tasks/current').status_code, 200)
        day = T.create_day(self.manager, date(2026, 11, 22))
        res = self.post(self.manager, f'/api/tasks/{day.id}/date', {'date': '2026-10-22'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['day']['date'], '2026-10-22')


class TaskDayArchive(Base):
    def test_new_day_tells_open_screens(self):
        old = T.create_day(self.manager, date(2026, 10, 21))
        before = TaskDay.objects.get(pk=old.pk).version
        T.create_day(self.manager, date(2026, 10, 22))
        old.refresh_from_db()
        self.assertTrue(old.archived)
        self.assertGreater(old.version, before)

    def test_warehouse_cannot_open_archived_day_but_sees_it_moved(self):
        old = T.create_day(self.manager, date(2026, 10, 21))
        T.create_day(self.manager, date(2026, 10, 22))
        self.assertEqual(self.get(self.sklad, f'/api/tasks/{old.id}').status_code, 403)
        self.assertEqual(self.post(self.sklad, f'/api/tasks/{old.id}/add', {'title': 'Склад'}).status_code, 403)
        res = self.get(self.sklad, f'/api/tasks/{old.id}/version')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['archived'])
        self.assertEqual(self.get(self.manager, f'/api/tasks/{old.id}').status_code, 200)


class WarehouseMonths(Base):
    def test_fines_only_current_month(self):
        self.assertEqual(self.get(self.sklad, '/api/fines/2026/10').status_code, 200)
        self.assertEqual(self.get(self.sklad, '/api/fines/2026/9').status_code, 403)
        self.assertEqual(self.get(self.manager, '/api/fines/2026/9').status_code, 200)

    def test_kpd_current_and_previous_month(self):
        body = {'assembler': self.ivan.id, 'text': '30'}
        self.assertEqual(self.post(self.sklad, '/api/kpd/cell', {**body, 'date': '2026-09-30'}).status_code, 200)
        self.assertEqual(self.post(self.sklad, '/api/kpd/cell', {**body, 'date': '2026-08-31'}).status_code, 403)
        self.assertEqual(self.get(self.sklad, '/api/kpd/2026/8').status_code, 403)
        self.assertEqual(self.post(self.manager, '/api/kpd/cell', {**body, 'date': '2026-08-31'}).status_code, 200)


class Kpd(Base):
    def test_out_of_range_month_not_saved(self):
        res = self.post(self.manager, '/api/kpd/cell', {'date': '2019-05-01', 'assembler': self.ivan.id, 'text': '5'})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(KpdCell.objects.exists())

    def test_number_with_comma_counted(self):
        self.assertEqual(K.parse('40, ушёл раньше'), (40, 'ушёл раньше'))
        self.assertEqual(K.parse('40ушёл'), (40, 'ушёл'))
        self.assertEqual(K.parse('не было'), (None, 'не было'))

    def test_version_changes_on_edit(self):
        v0 = K.month_version(2026, 10)
        K.set_cell('2026-10-20', self.ivan, '56', self.sklad)
        v1 = K.month_version(2026, 10)
        K.set_cell('2026-10-20', self.ivan, '', self.sklad)
        self.assertNotEqual(v0, v1)
        self.assertNotEqual(v1, K.month_version(2026, 10))


class Fines(Base):
    def test_fine_needs_person(self):
        res = self.post(self.manager, '/api/fines/2026/10/add', {'date': '2026-10-20', 'comment': 'Без ФИО'})
        self.assertEqual(res.status_code, 400)
        res = self.post(self.manager, '/api/fines/2026/10/add', {'date': '2026-10-20', 'assembler': self.ivan.id})
        eid = res.json()['table']['entries'][0]['id']
        self.assertEqual(self.post(self.manager, f'/api/fines/e/{eid}', {'assembler': None}).status_code, 400)


class BadRequests(Base):
    """Кривой запрос — понятная ошибка, а не «ошибка сервера» 500."""

    def test_bad_ids(self):
        sheet = S.create_sheet(self.manager, date(2026, 10, 21))
        day = T.create_day(self.manager, date(2026, 10, 21))
        cases = [
            (f'/api/sheets/{sheet.id}/board/sobrano/card', {'row': 'abc'}),
            (f'/api/sheets/{sheet.id}/board/sobrano/swap', {'car': 'abc'}),
            (f'/api/sheets/{sheet.id}/rows', {'fields': ['counterparty']}),
            (f'/api/tasks/{day.id}/add', {'car': 'abc', 'title': 'x'}),
            (f'/api/tasks/{day.id}/add', {'supplier': 'x'}),
            ('/api/fines/2026/10/add', {'date': '2026-10-20', 'assembler': 'x'}),
            ('/api/kpd/cell', {'date': '2026-10-20', 'assembler': 'x', 'text': '1'}),
            ('/api/firms/new', {'name': 'Строка', 'addresses': 'ул. Ленина'}),
        ]
        for url, body in cases:
            self.assertIn(self.post(self.manager, url, body).status_code, (400, 404), url)
        task = T.add_task(day, self.manager, {'title': 'Комус'})
        self.assertEqual(self.post(self.manager, f'/api/task/{task.id}', {'changes': ['done']}).status_code, 400)

    def test_messages_agree_in_gender(self):
        res = self.post(self.manager, '/api/kpd/cell', {'date': '2026-10-20', 'assembler': 999, 'text': '1'})
        self.assertIn('Кладовщик не найден.', res.json()['error'])

    def test_warehouse_swap_refused_before_lookup(self):
        sheet = S.create_sheet(self.manager, date(2026, 10, 21))
        res = self.post(self.sklad, f'/api/sheets/{sheet.id}/board/sobrano/swap', {'car': 999})
        self.assertEqual(res.status_code, 403)


class Directories(Base):
    def test_search_ignores_yo(self):
        Counterparty.objects.create(name='ИП Королёв')
        Supplier.objects.create(name='Тёплый дом')
        found = self.get(self.manager, '/api/firms?q=королев').json()['firms']
        self.assertEqual([f['name'] for f in found], ['ИП Королёв'])
        found = self.get(self.manager, '/api/firms?q=КОРОЛЁВ').json()['firms']
        self.assertEqual(len(found), 1)
        self.assertEqual(len(self.get(self.manager, '/api/suppliers?q=теплый').json()['suppliers']), 1)

    def test_duplicate_inn_and_car_refused(self):
        self.firm.inn = '6164000001'
        self.firm.save()
        res = self.post(self.manager, '/api/firms/new', {'name': 'Другая', 'inn': '6164000001'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('Союз', res.json()['error'])
        self.assertEqual(self.post(self.manager, '/api/cars', {'number': '829'}).status_code, 400)
        self.assertEqual(self.post(self.manager, f'/api/cars/{self.c829.id}', {'number': '829', 'driver': 'Новый'}).status_code, 200)


class Import(Base):
    def upload(self, rows):
        buf = io.StringIO()
        for r in rows:
            buf.write(';'.join(r) + '\n')
        f = SimpleUploadedFile('firms.csv', buf.getvalue().encode('utf-8'), content_type='text/csv')
        self.client.force_login(self.manager)
        return self.client.post('/api/firms/import', {'file': f, 'apply': '1'}).json()

    def test_same_name_different_inn_are_two_firms(self):
        res = self.upload([['Наименование', 'ИНН', 'Адрес доставки'],
                           ['ИП Петров', '7701234567', 'ул. Первая, 1'],
                           ['ИП Петров', '7709876543', 'ул. Вторая, 2']])
        self.assertEqual(res['summary']['new'], 2)
        self.assertEqual(res['done']['created'], 2)
        self.assertEqual(Counterparty.objects.filter(name='ИП Петров').count(), 2)

    def test_name_match_when_firm_has_no_inn(self):
        res = self.upload([['Наименование', 'ИНН'], ['ООО «Союз»', '6164000001']])
        self.assertEqual(res['summary']['new'], 0)
        self.firm.refresh_from_db()
        self.assertEqual(self.firm.inn, '6164000001')
        self.assertEqual(Counterparty.objects.count(), 1)

    def test_preview_counts_duplicates_in_file(self):
        res = self.upload([['Наименование', 'ИНН'], ['ИП Лис', ''], ['ИП Лис', ''], ['ИП Волк', ''], ['ИП Волк', '']])
        self.assertEqual(res['summary']['new'], 2)
        self.assertEqual(res['done']['created'], 2)

    def test_car_with_leading_zero(self):
        parsed = I.parse('Наименование;Машина\nИП Лис;66\n'.encode('utf-8'), 'f.csv')
        self.assertEqual(parsed['items'][0]['car'], self.c066.id)
        self.assertEqual(parsed['unknownCars'], [])


class Logistics(Base):
    def setUp(self):
        super().setUp()
        self.sheet = S.create_sheet(self.manager, date(2026, 10, 21))
        self.sobrano = S.board_of(self.sheet, Board.SOBRANO)
        self.a = S.add_row(self.sheet, self.manager, {'counterparty': self.firm.id, 'assembled': True})
        self.b = S.add_row(self.sheet, self.manager, {'counterparty': self.firm.id, 'assembled': True})

    def card(self, rid):
        for col in S.board_view(self.sobrano)['columns']:
            for c in col['cards']:
                if c['id'] == rid:
                    return col['key'], c

    def test_new_tag_stays_until_save(self):
        S.save_board(self.sobrano, self.manager)
        late = S.add_row(self.sheet, self.manager, {'counterparty': self.firm.id})
        S.update_row(late, self.manager, {'assembled': True})
        self.assertTrue(self.card(late.id)[1]['new'])
        # логист переставил другую карточку в этой колонке — метка «новая» остаётся
        S.move_cards(self.sobrano, {f'car:{self.c829.id}': [self.b.id, self.a.id, late.id]})
        self.assertTrue(self.card(late.id)[1]['new'])
        S.save_board(self.sobrano, self.manager)
        self.assertFalse(self.card(late.id)[1]['new'])

    def test_car_change_in_table_moves_card_unless_logist_moved_it(self):
        S.move_cards(self.sobrano, {f'car:{self.c829.id}': [self.b.id, self.a.id]})
        S.update_row(self.a, self.manager, {'car': self.c579.id})
        self.assertEqual(self.card(self.a.id)[0], f'car:{self.c579.id}')
        S.move_cards(self.sobrano, {f'car:{self.c066.id}': [self.b.id]})
        S.update_row(self.b, self.manager, {'car': self.c579.id})
        self.assertEqual(self.card(self.b.id)[0], f'car:{self.c066.id}')
        self.assertEqual(Placement.objects.filter(row=self.b).count(), 1)

    def test_places_edit_does_not_freeze_address(self):
        S.edit_card(self.sobrano, self.a, '', 7)
        key, card = self.card(self.a.id)
        self.assertEqual(card['addrEdit'], '')
        self.assertEqual(card['placesEdit'], 7)
        other = Address.objects.create(counterparty=self.firm, text='пр. Шолохова, 1')
        S.update_row(self.a, self.manager, {'address': other.id})
        self.assertEqual(self.card(self.a.id)[1]['addr'], 'пр. Шолохова, 1')


class Options(Base):
    def test_director_edits_variants(self):
        res = self.post(self.director, '/api/options', {'kind': 'note', 'name': 'Срочно', 'color': '#FFAA00', 'column': True})
        self.assertEqual(res.status_code, 200)
        opt = Option.objects.get(name='Срочно')
        self.assertEqual((opt.color, opt.own_column), ('#ffaa00', True))
        self.assertEqual(self.post(self.director, f'/api/options/{opt.id}', {'active': False, 'color': ''}).status_code, 200)
        opt.refresh_from_db()
        self.assertFalse(opt.active)

    def test_bad_variants_refused(self):
        self.post(self.director, '/api/options', {'kind': 'note', 'name': 'Срочно'})
        self.assertEqual(self.post(self.director, '/api/options', {'kind': 'note', 'name': 'срочно'}).status_code, 400)
        self.assertEqual(self.post(self.director, '/api/options', {'kind': 'note', 'name': 'X', 'color': 'red;'}).status_code, 400)
        self.assertEqual(self.post(self.director, '/api/options', {'kind': 'car', 'name': 'X'}).status_code, 400)
        res = self.post(self.director, '/api/options', {'kind': 'assembly', 'name': 'Ждём товар', 'column': True})
        self.assertFalse(res.json()['option']['column'])

    def test_only_director_and_assistant(self):
        for user in (self.manager, self.sklad):
            self.assertEqual(self.post(user, '/api/options', {'kind': 'note', 'name': 'X'}).status_code, 403)
        self.assertEqual(self.post(self.assistant, '/api/options', {'kind': 'note', 'name': 'X'}).status_code, 200)


class Login(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user('ivan', password='x' * 10, role=roles.MANAGER)

    def attempt(self, ip, password='wrong'):
        return self.client.post('/login/', {'username': 'ivan', 'password': password}, HTTP_X_REAL_IP=ip)

    def test_lockout_is_per_address_behind_nginx(self):
        for _ in range(8):
            self.attempt('1.1.1.1')
        res = self.attempt('1.1.1.1', 'x' * 10)
        self.assertEqual(res.status_code, 200)  # заблокирован, остался на странице входа
        res = self.attempt('2.2.2.2', 'x' * 10)
        self.assertEqual(res.status_code, 302)  # с другого адреса тот же логин входит


class FoundInBrowser(Base):
    def test_used_address_hidden_not_erased(self):
        sheet = S.create_sheet(self.manager, date(2026, 10, 21))
        row = S.add_row(sheet, self.manager, {'counterparty': self.firm.id})
        self.assertEqual(self.post(self.manager, f'/api/addresses/{self.addr.id}/delete', {}).status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.address.text, 'ул. Закруткина, 42')
        firm = self.get(self.manager, f'/api/firms/{self.firm.id}').json()['firm']
        self.assertEqual(firm['addresses'], [])
        # новая накладная скрытый адрес уже не подставляет
        self.assertIsNone(S.add_row(sheet, self.manager, {'counterparty': self.firm.id}).address)
        unused = Address.objects.create(counterparty=self.firm, text='ул. Пустая, 1')
        self.post(self.manager, f'/api/addresses/{unused.id}/delete', {})
        self.assertFalse(Address.objects.filter(pk=unused.pk).exists())

    def test_car_out_of_work_not_put_automatically(self):
        self.c829.active = False
        self.c829.save()
        sheet = S.create_sheet(self.manager, date(2026, 10, 21))
        row = S.add_row(sheet, self.manager, {'counterparty': self.firm.id})
        self.assertIsNone(row.car)
        self.assertEqual(S.sheet_problems(sheet)['no_car'], [1])

    def test_same_date_message_points_to_open_table(self):
        S.create_sheet(self.manager, date(2026, 10, 22))
        res = self.post(self.manager, '/api/sheets', {'date': '2026-10-22'})
        self.assertIn('сейчас открыта', res.json()['error'])
        self.assertNotIn('архив', res.json()['error'])


class SecondCheck(Base):
    """Находки проверки на боевом сервере (02.11.2026)."""

    def make_row(self):
        sheet = S.create_sheet(self.manager, TODAY + timedelta(days=1))
        return S.add_row(sheet, self.manager, {'counterparty': self.firm.pk})

    def test_control_characters_do_not_crash(self):
        for body in ({'name': 'a\u0000b'}, {'name': '\ud800x'}):
            r = self.post(self.manager, '/api/firms/new', body)
            self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(any('\u0000' in n for n in Counterparty.objects.values_list('name', flat=True)))
        self.assertEqual(self.get(self.manager, '/api/firms?q=%00').status_code, 200)

    def test_infinity_and_odd_types_are_400_not_500(self):
        row = self.make_row()
        self.client.force_login(self.manager)
        for raw in ('{"changes": {"car": Infinity}}', '{"changes": {"places": NaN}}', '{"changes": {"car": [1]}}'):
            r = self.client.post(f'/api/rows/{row.pk}', raw, content_type='application/json')
            self.assertLess(r.status_code, 500, raw)
        r = self.post(self.director, '/api/options', {'kind': []})
        self.assertLess(r.status_code, 500)

    def test_row_update_locks_and_keeps_both_changes(self):
        row = self.make_row()
        stale = type(row).objects.get(pk=row.pk)  # устаревшая копия, как у второго запроса
        S.update_row(row, self.manager, {'places': 7})
        S.update_row(stale, self.sklad, {'assembled': True})
        row.refresh_from_db()
        self.assertEqual(row.places, 7)
        self.assertTrue(row.assembled)

    def test_admin_is_closed(self):
        self.assertEqual(self.client.get('/admin/login/').status_code, 404)

    def test_xlsx_zip_bomb_is_refused(self):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('xl/big.xml', b'0' * (101 * 1024 * 1024))
        with self.assertRaises(S.Problem):
            I.read_rows(buf.getvalue(), 'bomb.xlsx')

    def test_firms_with_shared_inn_can_still_be_edited(self):
        a = Counterparty.objects.create(name='Головная', inn='3446024300')
        b = Counterparty.objects.create(name='Филиал', inn='3446024300')
        r = self.post(self.manager, f'/api/firms/{b.pk}', {'name': 'Филиал', 'inn': '3446024300', 'phones': '123'})
        self.assertEqual(r.status_code, 200, r.content)
        b.refresh_from_db()
        self.assertEqual(b.phones, '123')
        r = self.post(self.manager, '/api/firms/new', {'name': 'Третья', 'inn': '3446024300'})
        self.assertEqual(r.status_code, 400)


class WarehouseLeastPrivilege(Base):
    """Права склада (решение 03.11.2026): заказчик писал «склад меняет таблицу сборки (включая контрагента и строки)»."""

    def setUp(self):
        super().setUp()
        self.sheet = S.create_sheet(self.manager, TODAY + timedelta(days=1))
        self.row = S.add_row(self.sheet, self.manager, {'counterparty': self.firm.pk})

    def test_warehouse_changes_the_table(self):
        opt = Option.objects.filter(kind=Option.ASSEMBLY).first()
        for changes in ({'assembled': True}, {'places': 4}, {'assembler': self.ivan.pk}, {'car': self.c579.pk},
                        {'note_text': 'позвонить'}, {'assembly': opt.pk if opt else None}):
            r = self.post(self.sklad, f'/api/rows/{self.row.pk}', {'changes': changes})
            self.assertEqual(r.status_code, 200, (changes, r.content))
        r = self.post(self.sklad, f'/api/sheets/{self.sheet.pk}/rows', {'fields': {'counterparty': self.firm.pk}})
        self.assertEqual(r.status_code, 200, r.content)

    def test_warehouse_can_delete_rows(self):
        # Заказчик 10.11.2026: склад тоже вносит накладные и должен уметь удалить лишнюю, в том числе чужую.
        self.assertEqual(self.post(self.sklad, f'/api/rows/{self.row.pk}/delete', {}).status_code, 200)
        own = self.post(self.sklad, f'/api/sheets/{self.sheet.pk}/rows', {'fields': {'counterparty': self.firm.pk}}).json()
        self.assertEqual(self.post(self.sklad, f"/api/rows/{own['row']['id']}/delete", {}).status_code, 200)

    def test_warehouse_creates_firm_but_cannot_edit_directories(self):
        self.assertEqual(self.post(self.sklad, '/api/firms/new', {'name': 'Новая фирма'}).status_code, 200)
        self.assertEqual(self.post(self.sklad, f'/api/firms/{self.firm.pk}', {'phones': '1'}).status_code, 403)
        self.assertEqual(self.post(self.sklad, '/api/suppliers/new', {'name': 'Пост'}).status_code, 403)
        # Новое имя сборщика склад вписать может (нужно для «кто собрал» и КПД), менять и отключать сборщиков нет.
        self.assertEqual(self.post(self.sklad, '/api/assemblers', {'name': 'Новый'}).status_code, 200)
        asm = Assembler.objects.get(name='Новый')
        self.assertEqual(self.post(self.sklad, f'/api/assemblers/{asm.pk}', {'name': 'Другой'}).status_code, 403)
        perms = self.get(self.sklad, '/api/bootstrap').json()['me']['perms']
        self.assertTrue(perms['firms_create'])
        self.assertFalse(perms['firms_edit'] or perms['cars_edit'] or perms['sheet_create'])
        self.assertFalse(perms['logistics_edit'] or perms['fines_edit'] or perms['archive_other'])

    def test_manager_and_sales_still_do_everything_in_the_table(self):
        sales = User.objects.create_user('p', password='x' * 10, role=roles.SALES)
        for user in (self.manager, sales):
            r = self.post(user, f'/api/rows/{self.row.pk}', {'changes': {'car': self.c579.pk, 'note_text': 'ok'}})
            self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.post(self.manager, f'/api/rows/{self.row.pk}/delete', {}).status_code, 200)


class SecondRound(Base):
    def setUp(self):
        super().setUp()
        self.sheet = S.create_sheet(self.manager, TODAY + timedelta(days=1))
        self.row = S.add_row(self.sheet, self.manager, {'counterparty': self.firm.pk})

    def test_warehouse_cannot_change_firm_car_through_remember(self):
        r = self.post(self.sklad, f'/api/rows/{self.row.pk}', {'changes': {'car': self.c579.pk}, 'rememberCar': True})
        self.assertEqual(r.status_code, 200, r.content)
        self.firm.refresh_from_db(); self.row.refresh_from_db()
        self.assertEqual(self.firm.car_id, self.c829.pk)
        self.assertEqual(self.row.car_id, self.c579.pk)
        r = self.post(self.manager, f'/api/rows/{self.row.pk}', {'changes': {'car': self.c066.pk}, 'rememberCar': True})
        self.firm.refresh_from_db()
        self.assertEqual(self.firm.car_id, self.c066.pk)

    def test_copied_row_keeps_original_author(self):
        target = S.create_sheet(self.manager, TODAY + timedelta(days=2))
        copy = S.copy_row(self.row, target, self.sklad)
        self.assertEqual(copy.created_by_id, self.manager.pk)


class PasswordPageExit(TestCase):
    def test_forced_password_page_has_logout(self):
        u = User.objects.create_user('tmp', password='Temp-pass-1', role=roles.MANAGER)
        u.must_change_password = True
        u.save()
        self.client.force_login(u)
        page = self.client.get('/password/')
        self.assertContains(page, 'action="/logout/"')
        self.client.post('/logout/')
        self.assertEqual(self.client.get('/api/bootstrap').status_code, 401)


class FirmInfo(Base):
    def test_info_field_saved_and_returned(self):
        r = self.post(self.manager, f'/api/firms/{self.firm.pk}', {'info': 'Позвонить за 30 минут. Анна 8-918-512-93-00'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['firm']['info'], 'Позвонить за 30 минут. Анна 8-918-512-93-00')
        self.assertEqual(self.post(self.sklad, f'/api/firms/{self.firm.pk}', {'info': 'x'}).status_code, 403)

    def test_import_reads_additional_info_column(self):
        csv = 'Контрагент;ИНН;Дополнительная информация\nООО Новая;7700000001;Звонить Ивану 8-900-000-00-00\n'
        data = I.parse(csv.encode('utf-8'), 'f.csv')
        self.assertEqual(data['items'][0]['info'], 'Звонить Ивану 8-900-000-00-00')
        I.apply(data['items'])
        self.assertEqual(Counterparty.objects.get(name='ООО Новая').info, 'Звонить Ивану 8-900-000-00-00')

    def test_info_shown_on_logistics_card(self):
        self.firm.info = 'Лифт не работает'
        self.firm.save()
        sheet = S.create_sheet(self.manager, TODAY + timedelta(days=1))
        S.add_row(sheet, self.manager, {'counterparty': self.firm.pk})
        board = self.get(self.manager, f'/api/sheets/{sheet.pk}/board/den').json()['board']
        cards = [c for col in board['columns'] for c in col['cards']]
        self.assertTrue(any(c.get('info') == 'Лифт не работает' for c in cards))

    def test_all_roles_see_info_and_strip_works_when_rule_is_off(self):
        from sklad.api import strip_info
        self.firm.info = 'За 30 минут позвонить'
        self.firm.save()
        for user in (self.manager, self.sklad):
            r = self.get(user, f'/api/firms/{self.firm.pk}').json()
            self.assertEqual(r['firm']['info'], 'За 30 минут позвонить')
        self.assertNotIn('info', strip_info({'firm': {'name': 'x', 'info': 'секрет'}})['firm'])


class TenthNovember(Base):
    """Правки заказчика 10.11.2026."""

    def setUp(self):
        super().setUp()
        self.sheet = S.create_sheet(self.manager, TODAY + timedelta(days=1))
        self.row = S.add_row(self.sheet, self.manager, {'counterparty': self.firm.pk, 'car': self.c829.pk})
        self.petr = Assembler.objects.create(name='Пётр')

    def test_two_assemblers_on_one_row(self):
        r = self.post(self.sklad, f'/api/rows/{self.row.pk}', {'changes': {'assembler': self.ivan.pk}})
        r = self.post(self.sklad, f'/api/rows/{self.row.pk}', {'changes': {'assembler2': self.petr.pk}})
        self.assertEqual(r.status_code, 200, r.content)
        row = r.json()['row']
        self.assertEqual((row['who'], row['who2']), (self.ivan.pk, self.petr.pk))
        self.assertEqual(row['firmInn'], '')
        r = self.post(self.sklad, f'/api/rows/{self.row.pk}', {'changes': {'assembler2': None}})
        self.assertIsNone(r.json()['row']['who2'])

    def test_no_such_assembler2_is_error_not_500(self):
        r = self.post(self.sklad, f'/api/rows/{self.row.pk}', {'changes': {'assembler2': 99999}})
        self.assertEqual(r.status_code, 400)

    def test_net_nakladnoy_is_red(self):
        opt = Option.objects.filter(kind=Option.ASSEMBLY, name='Нет накладной').first()
        if opt:
            self.assertEqual(opt.color, '#f26b5f')

    def _docx_text(self, resp):
        import zipfile
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertIn('wordprocessingml', resp['Content-Type'])
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            return z.read('word/document.xml').decode('utf-8')

    def test_tasks_docx(self):
        day = T.create_day(self.manager, TODAY + timedelta(days=1))
        T.add_task(day, self.manager, {'title': 'Рынок', 'text': 'Купить кофе 6 шт', 'car': self.c829.pk})
        xml = self._docx_text(self.get(self.manager, f'/api/tasks/{day.pk}/docx'))
        self.assertIn('Купить кофе 6 шт', xml)
        self.assertIn('829', xml)
        self.assertEqual(self.client.get(f'/api/tasks/{day.pk}/docx').status_code, 200)  # вошёл тот же клиент

    def test_docx_requires_login_and_archive_rights(self):
        day = T.create_day(self.manager, TODAY + timedelta(days=1))
        T.create_day(self.manager, TODAY + timedelta(days=2))  # прошлый день ушёл в архив
        self.client.logout()
        self.assertEqual(self.client.get(f'/api/tasks/{day.pk}/docx').status_code, 401)
        self.assertEqual(self.get(self.sklad, f'/api/tasks/{day.pk}/docx').status_code, 403)

    def test_board_docx_has_manager_and_hides_commercial_notes(self):
        self.firm.info = 'Позвонить за 30 минут\nСкидка 15% постоянному клиенту'
        self.firm.phones = '8-900-000-00-00'
        self.firm.save()
        S.update_row(self.row, self.manager, {'assembled': True})
        xml = self._docx_text(self.get(self.manager, f'/api/sheets/{self.sheet.pk}/board/den/docx'))
        self.assertIn('Позвонить за 30 минут', xml)
        self.assertNotIn('Скидка', xml)
        self.assertIn(self.manager.display_name, xml)

    def test_commercial_words_not_in_word_file(self):
        from sklad import docx as D
        info = '\n'.join(['Позвонить за 30 минут', 'Прайс оптовый', 'Рассрочка 14 дней', 'Стоимость доставки 500 руб', 'Карта клиента №5',
                          'Задолженность 10000', 'безнал', 'НДС 22%', 'Скидка 5%', 'Оплата по факту', 'Выставлять счёт заранее'])
        self.assertEqual(D.delivery_info(info), 'Позвонить за 30 минут')

    def test_docx_survives_control_characters(self):
        import zipfile, xml.dom.minidom
        from sklad import docx as D
        data = D.build('Т\x01итул', '', [{'heading': 'М\x02', 'headers': ['а'], 'rows': [['x\x00y\ud800']], 'widths': [1000]}])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml.dom.minidom.parseString(z.read('word/document.xml'))

    def test_carry_flag_only_true_carries(self):
        day = T.create_day(self.manager, TODAY + timedelta(days=1))
        T.add_task(day, self.manager, {'title': 'Рынок', 'text': 'x'})
        for value in ('false', 'no', 0, None):
            r = self.post(self.manager, '/api/tasks/new', {'date': (TODAY + timedelta(days=2)).isoformat(), 'carry': value})
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(len(r.json()['day']['tasks']), 0, value)
            TaskDay.objects.filter(date=TODAY + timedelta(days=2)).delete()

    def test_commercial_filter_keeps_normal_delivery_words(self):
        from sklad import docx as D
        keep = ['Привозить картон', 'Есть наличие товара', 'Звонить долго не берут', 'Центр города', 'счетчик на воротах']
        drop = ['Карта клиента №5', 'Оплата наличными', 'наличка', 'Долг 5000', 'Скидка 5%', 'цены база', 'Счёт выставлять']
        for line in keep:
            self.assertEqual(D.delivery_info(line), line, line)
        for line in drop:
            self.assertEqual(D.delivery_info(line), '', line)
        long_line = 'Заезд со двора ' + 'очень длинное пояснение ' * 12
        self.assertTrue(D.delivery_info(long_line).endswith('…'))
