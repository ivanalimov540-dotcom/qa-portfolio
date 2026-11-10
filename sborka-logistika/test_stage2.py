"""Задания водителям, штрафы, КПД и загрузка фирм из Excel."""

import io
import json
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook

from accounts import roles
from sklad import fines as F
from sklad import importer as I
from sklad import kpd as K
from sklad import services as S
from sklad import tasks as T
from sklad.models import Address, Assembler, Car, Counterparty, Supplier

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.c829 = Car.objects.create(number='829', driver='Джафаров С.М.')
        self.c579 = Car.objects.create(number='579', driver='Баташов А.И.', sort=1)
        self.parshin = Supplier.objects.create(name='ИП Паршин', address='ул. Вавилова, 62')
        self.ivan = Assembler.objects.create(name='Иван')
        self.oleg = Assembler.objects.create(name='Олег', sort=1)
        self.manager = User.objects.create_user('m', password='x' * 10, role=roles.MANAGER)
        self.sklad = User.objects.create_user('s', password='x' * 10, role=roles.WAREHOUSE)
        self.director = User.objects.create_user('d', password='x' * 10, role=roles.DIRECTOR)
        self.day = date(2026, 10, 20)
        # «Сегодня» в тестах всегда 21.10.2026, чтобы проверки дат не зависели от дня запуска.
        for target in ('sklad.services.today', 'sklad.tasks.today'):
            patcher = mock.patch(target, return_value=date(2026, 10, 21))
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, user, url, body=None):
        self.client.force_login(user)
        return self.client.post(url, json.dumps(body or {}), content_type='application/json')


class DriverTasks(Base):
    def test_new_day_archives_previous_and_can_carry_open_tasks(self):
        first = T.create_day(self.manager, self.day)
        T.add_task(first, self.manager, {'car': self.c829.id, 'supplier': self.parshin.id, 'text': 'счёт 1452'})
        done = T.add_task(first, self.manager, {'car': self.c829.id, 'title': 'Рынок'})
        T.update_task(done, {'done': True})
        second = T.create_day(self.manager, self.day + timedelta(days=1), carry=True)
        first.refresh_from_db()
        self.assertTrue(first.archived)
        self.assertEqual([t.supplier for t in second.tasks.all()], [self.parshin])

    def test_task_needs_place(self):
        d = T.create_day(self.manager, self.day)
        with self.assertRaises(S.Problem):
            T.add_task(d, self.manager, {'car': self.c829.id, 'text': 'что-то'})

    def test_move_changes_car_and_order(self):
        d = T.create_day(self.manager, self.day)
        a = T.add_task(d, self.manager, {'car': self.c829.id, 'title': 'Рынок'})
        b = T.add_task(d, self.manager, {'car': self.c829.id, 'supplier': self.parshin.id})
        T.move_tasks(d, {f'car:{self.c579.id}': [b.id, a.id], f'car:{self.c829.id}': []})
        view = {c['key']: [x['id'] for x in c['cards']] for c in T.day_view(d)['columns']}
        self.assertEqual(view[f'car:{self.c579.id}'], [b.id, a.id])
        self.assertEqual(view[f'car:{self.c829.id}'], [])

    def test_done_sets_time(self):
        d = T.create_day(self.manager, self.day)
        t = T.add_task(d, self.manager, {'car': self.c829.id, 'title': 'Рынок'})
        T.update_task(t, {'done': True})
        self.assertIsNotNone(t.done_at)

    def test_car_chosen_in_list_puts_task_in_car_column(self):
        d = T.create_day(self.manager, self.day)
        t = T.add_task(d, self.manager, {'supplier': self.parshin.id, 'text': 'хозтовары'})
        T.update_task(t, {'car': self.c579.id})
        view = {c['key']: [x['id'] for x in c['cards']] for c in T.day_view(d)['columns']}
        self.assertEqual(view[f'car:{self.c579.id}'], [t.id])
        self.assertEqual([x['id'] for x in T.day_view(d)['tasks']], [t.id])

    def test_warehouse_fills_tasks_too(self):
        d = T.create_day(self.manager, self.day)
        self.assertEqual(self.post(self.sklad, f'/api/tasks/{d.id}/add', {'title': 'Рынок'}).status_code, 200)


class Fines(Base):
    def test_list_of_entries_with_totals(self):
        F.add_entry(self.manager, 2026, 10, {'date': '2026-10-19', 'assembler': self.ivan.id, 'comment': 'Не догрузил 2 места'})
        F.add_entry(self.manager, 2026, 10, {'date': '2026-10-05', 'assembler': self.oleg.id, 'comment': 'Перепутал адрес'})
        F.add_entry(self.manager, 2026, 10, {'date': '2026-10-20', 'assembler': self.ivan.id, 'comment': 'Опоздал'})
        view = F.month_view(2026, 10)
        self.assertEqual([e['date'] for e in view['entries']], ['2026-10-05', '2026-10-19', '2026-10-20'])
        self.assertEqual(view['totals'], [{'name': 'Иван', 'count': 2}, {'name': 'Олег', 'count': 1}])

    def test_date_must_be_in_month(self):
        with self.assertRaises(S.Problem):
            F.add_entry(self.manager, 2026, 10, {'date': '2026-11-01', 'assembler': self.ivan.id})

    def test_edit_and_delete(self):
        e = F.add_entry(self.manager, 2026, 10, {'date': '2026-10-19', 'assembler': self.ivan.id})
        F.update_entry(e, {'assembler': self.oleg.id, 'comment': 'Не тот товар'})
        self.assertEqual(F.month_view(2026, 10)['entries'][0]['name'], 'Олег')
        F.delete_entry(e)
        self.assertEqual(F.month_view(2026, 10)['entries'], [])

    def test_only_managers_and_director_write(self):
        body = {'date': '2026-10-19', 'assembler': self.ivan.id}
        self.assertEqual(self.post(self.sklad, '/api/fines/2026/10/add', body).status_code, 403)
        self.assertEqual(self.post(self.director, '/api/fines/2026/10/add', body).status_code, 200)
        self.client.force_login(self.sklad)
        self.assertEqual(self.client.get('/api/fines/2026/10').status_code, 200)


class Kpd(Base):
    def test_lines_typed_by_hand_with_totals(self):
        K.set_cell('2026-10-19', self.ivan, '56', self.sklad)
        K.set_cell('2026-10-20', self.ivan, '40 ушёл раньше', self.sklad)
        K.set_cell('2026-10-19', self.oleg, 'не было', self.sklad)
        K.set_cell('2026-10-20', self.oleg, '30', self.sklad)
        view = K.month_view(2026, 10)
        day19 = next(d for d in view['days'] if d['date'] == '2026-10-19')
        self.assertEqual(day19['cells'][self.ivan.id], {'v': 56, 'note': ''})
        self.assertEqual(day19['cells'][self.oleg.id], {'v': None, 'note': 'не было'})
        self.assertEqual(day19['total'], 56)
        self.assertEqual(view['totals'], {self.ivan.id: 96, self.oleg.id: 30})
        self.assertEqual(view['grand'], 126)
        K.set_cell('2026-10-19', self.ivan, '', self.sklad)
        self.assertEqual(K.month_view(2026, 10)['totals'][self.ivan.id], 40)

    def test_warehouse_can_write(self):
        res = self.post(self.sklad, '/api/kpd/cell', {'date': '2026-10-18', 'assembler': self.oleg.id, 'text': 'заболел'})
        self.assertEqual(res.status_code, 200)


class BoardEdits(Base):
    def test_address_and_places_changed_only_on_board(self):
        firm = Counterparty.objects.create(name='ООО «Союз»', car=self.c829, phones='+7 863 200-00-00')
        Address.objects.create(counterparty=firm, text='ул. Закруткина, 42')
        sheet = S.create_sheet(self.manager, self.day)
        row = S.add_row(sheet, self.manager, {'counterparty': firm.id, 'assembled': True, 'places': 4})
        board = S.board_of(sheet, 'sobrano')
        S.edit_card(board, row, 'пр. Стачки, 10', 6)
        card = next(c for col in S.board_view(board)['columns'] for c in col['cards'])
        self.assertEqual((card['addr'], card['places'], card['edited'], card['phone']), ('пр. Стачки, 10', 6, True, '+7 863 200-00-00'))
        row.refresh_from_db()
        self.assertEqual((row.address.text, row.places), ('ул. Закруткина, 42', 4))
        den = next(c for col in S.board_view(S.board_of(sheet, 'den'))['columns'] for c in col['cards'])
        self.assertEqual((den['addr'], den['places']), ('ул. Закруткина, 42', 4))
        S.edit_card(board, row, '', '')
        card = next(c for col in S.board_view(board)['columns'] for c in col['cards'])
        self.assertEqual((card['addr'], card['places'], card['edited']), ('ул. Закруткина, 42', 4, False))


def xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FirmImport(Base):
    ONE_C = [
        ['Контрагенты'],
        [],
        ['Наименование', 'Полное наименование', 'ИНН', 'КПП', 'Юридический адрес', 'Адрес доставки', 'Телефон', 'Машина'],
        ['ООО «Союз»', 'Общество с ограниченной ответственностью «Союз»', 6161234567, '616101001', 'ул. Ленина, 1',
         'ул. Закруткина, 42', '+7 863 200-00-00', 829],
        ['ИП Ломов', 'Индивидуальный предприниматель Ломов', '616112345678', '', '', 'пр. Стачки, 10; ул. Нансена, 5', '', '999'],
        ['', '', '', '', '', '', '', ''],
    ]

    def test_detects_1c_columns(self):
        parsed = I.parse(xlsx(self.ONE_C), 'контрагенты.xlsx')
        self.assertEqual(parsed['columns']['name'], '«Наименование»')
        self.assertEqual(parsed['columns']['address'], '«Адрес доставки»')
        first, second = parsed['items']
        self.assertEqual((first['name'], first['inn'], first['addresses'], first['car']),
                         ('ООО «Союз»', '6161234567', ['ул. Закруткина, 42'], self.c829.id))
        self.assertEqual(second['addresses'], ['пр. Стачки, 10', 'ул. Нансена, 5'])
        self.assertEqual(parsed['unknownCars'], ['999'])

    def test_apply_creates_then_only_fills_gaps(self):
        firm = Counterparty.objects.create(name='ИП Ломов', phones='есть свой')
        Address.objects.create(counterparty=firm, text='пр. Стачки, 10')
        done = I.apply(I.parse(xlsx(self.ONE_C), 'x.xlsx')['items'])
        self.assertEqual(done, {'created': 1, 'updated': 1, 'addresses': 2})
        firm.refresh_from_db()
        self.assertEqual((firm.inn, firm.phones), ('616112345678', 'есть свой'))
        self.assertEqual(sorted(firm.addresses.values_list('text', flat=True)), ['пр. Стачки, 10', 'ул. Нансена, 5'])
        again = I.apply(I.parse(xlsx(self.ONE_C), 'x.xlsx')['items'])
        self.assertEqual(again, {'created': 0, 'updated': 0, 'addresses': 0})

    def test_csv_in_windows_encoding(self):
        data = 'Контрагент;ИНН;Адрес\nООО «Вектор»;6160000001;ул. Пушкинская, 7\n'.encode('cp1251')
        parsed = I.parse(data, 'list.csv')
        self.assertEqual(parsed['items'][0]['addresses'], ['ул. Пушкинская, 7'])

    def test_old_xls_and_missing_name_explained(self):
        with self.assertRaises(S.Problem):
            I.parse(b'\xd0\xcf\x11\xe0', 'old.xls')
        with self.assertRaises(S.Problem):
            I.parse(xlsx([['ИНН', 'Телефон'], ['6161234567', '1']]), 'x.xlsx')

    def test_upload_preview_then_apply(self):
        self.client.force_login(self.manager)
        f = lambda: SimpleUploadedFile('k.xlsx', xlsx(self.ONE_C))  # noqa: E731
        res = self.client.post('/api/firms/import', {'file': f(), 'apply': '0'}).json()
        self.assertEqual(res['summary']['total'], 2)
        self.assertFalse(Counterparty.objects.exists())
        res = self.client.post('/api/firms/import', {'file': f(), 'apply': '1'}).json()
        self.assertEqual(res['done']['created'], 2)
        self.client.force_login(self.sklad)
        self.assertEqual(self.client.post('/api/firms/import', {'file': f()}).status_code, 403)


class SupplierContacts(Base):
    def test_contact_shown_in_task_and_loader_fills_only_gaps(self):
        import json as _json
        import tempfile
        from django.core.management import call_command
        own = Supplier.objects.create(name='ООО Папирус', address='вписали сами')
        data = {'suppliers': [{'name': 'ООО Папирус', 'address': 'ул. Доватора, 158/11', 'contact': 'Кристина'},
                              {'name': 'Комус', 'address': 'ул. Геологическая, 5', 'contact': ''}]}
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            _json.dump(data, f, ensure_ascii=False)
        call_command('load_initial', file=f.name, stdout=io.StringIO())
        call_command('load_initial', file=f.name, stdout=io.StringIO())
        own.refresh_from_db()
        self.assertEqual((own.address, own.contact), ('вписали сами', 'Кристина'))
        self.assertEqual(Supplier.objects.filter(name='Комус').count(), 1)
        d = T.create_day(self.manager, self.day)
        T.add_task(d, self.manager, {'supplier': own.id})
        self.assertEqual(T.day_view(d)['tasks'][0]['contact'], 'Кристина')
