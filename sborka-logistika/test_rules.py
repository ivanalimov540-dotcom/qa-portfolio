"""Главные правила заказчика: таблица на день, архив, машина обязательна, логистика не трогает таблицу сборки, права."""

import json
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts import roles
from sklad import services as S
from sklad.models import Address, Board, Car, Counterparty, Option, Sheet

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.c829 = Car.objects.create(number='829', driver='Джафаров С.М.', sort=0)
        self.c579 = Car.objects.create(number='579', driver='Баташов А.И.', sort=1)
        self.firm = Counterparty.objects.create(name='ООО «Союз»', car=self.c829)
        self.addr = Address.objects.create(counterparty=self.firm, text='ул. Закруткина, 42')
        self.manager = User.objects.create_user('m', password='x' * 10, first_name='Менеджер', role=roles.MANAGER)
        self.sklad = User.objects.create_user('s', password='x' * 10, first_name='Склад', role=roles.WAREHOUSE)
        self.director = User.objects.create_user('d', password='x' * 10, first_name='Директор', role=roles.DIRECTOR)
        self.day = date(2026, 10, 20)
        # «Сегодня» в тестах всегда 21.10.2026, чтобы проверки дат не зависели от дня запуска.
        for target in ('sklad.services.today', 'sklad.tasks.today'):
            patcher = mock.patch(target, return_value=date(2026, 10, 21))
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, user, url, body=None):
        self.client.force_login(user)
        return self.client.post(url, json.dumps(body or {}), content_type='application/json')

    def get(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)


class SheetRules(Base):
    def test_new_sheet_sends_previous_to_archive(self):
        first = S.create_sheet(self.manager, self.day)
        second = S.create_sheet(self.manager, self.day + timedelta(days=1))
        first.refresh_from_db()
        self.assertTrue(first.archived)
        self.assertFalse(second.archived)
        self.assertEqual(S.current_sheet(), second)
        self.assertEqual(second.boards.count(), 2)

    def test_same_date_and_earlier_date_refused(self):
        S.create_sheet(self.manager, self.day)
        with self.assertRaises(S.Problem):
            S.create_sheet(self.manager, self.day)
        with self.assertRaises(S.Problem):
            S.create_sheet(self.manager, self.day - timedelta(days=1))

    def test_firm_brings_its_car_and_address(self):
        sheet = S.create_sheet(self.manager, self.day)
        row = S.add_row(sheet, self.manager, {'counterparty': self.firm.id})
        self.assertEqual(row.car, self.c829)
        self.assertEqual(row.address, self.addr)
        self.assertEqual(row.created_by, self.manager)

    def test_car_change_remembered_only_when_asked(self):
        sheet = S.create_sheet(self.manager, self.day)
        row = S.add_row(sheet, self.manager, {'counterparty': self.firm.id})
        S.update_row(row, self.manager, {'car': self.c579.id})
        self.firm.refresh_from_db()
        self.assertEqual(self.firm.car, self.c829)
        S.update_row(row, self.manager, {'car': self.c579.id}, remember_car=True)
        self.firm.refresh_from_db()
        self.assertEqual(self.firm.car, self.c579)

    def test_save_check_lists_rows_without_car(self):
        sheet = S.create_sheet(self.manager, self.day)
        S.add_row(sheet, self.manager, {'counterparty': self.firm.id})
        no_car = Counterparty.objects.create(name='ИП Ломов')
        S.add_row(sheet, self.manager, {'counterparty': no_car.id})
        S.add_row(sheet, self.manager, {})
        self.assertEqual(S.sheet_problems(sheet), {'no_firm': [3], 'no_car': [2]})

    def test_assembled_time_set_and_cleared(self):
        sheet = S.create_sheet(self.manager, self.day)
        row = S.add_row(sheet, self.manager, {'counterparty': self.firm.id})
        S.update_row(row, self.sklad, {'assembled': True, 'places': 4})
        self.assertIsNotNone(row.assembled_at)
        S.update_row(row, self.sklad, {'assembled': False})
        self.assertIsNone(row.assembled_at)

    def test_firm_search_is_case_insensitive_in_russian(self):
        res = self.get(self.manager, '/api/firms?q=союз').json()
        self.assertEqual([f['name'] for f in res['firms']], ['ООО «Союз»'])


class BoardRules(Base):
    def setUp(self):
        super().setUp()
        self.sheet = S.create_sheet(self.manager, self.day)
        self.done = S.add_row(self.sheet, self.manager, {'counterparty': self.firm.id, 'assembled': True, 'places': 4})
        self.waiting = S.add_row(self.sheet, self.manager, {'counterparty': self.firm.id})
        self.sobrano = S.board_of(self.sheet, Board.SOBRANO)
        self.den = S.board_of(self.sheet, Board.DEN)

    def cards(self, board):
        view = S.board_view(board)
        return {col['key']: [c['id'] for c in col['cards']] for col in view['columns']}

    def test_sobrano_shows_only_assembled_den_shows_all(self):
        self.assertEqual(self.cards(self.sobrano)[f'car:{self.c829.id}'], [self.done.id])
        self.assertEqual(self.cards(self.den)[f'car:{self.c829.id}'], [self.done.id, self.waiting.id])

    def test_moves_live_only_on_board(self):
        S.move_cards(self.sobrano, {f'car:{self.c579.id}': [self.done.id], f'car:{self.c829.id}': []})
        self.done.refresh_from_db()
        self.assertEqual(self.done.car, self.c829)  # основная таблица не изменилась
        self.assertEqual(self.cards(self.sobrano)[f'car:{self.c579.id}'], [self.done.id])
        self.assertEqual(self.cards(self.den)[f'car:{self.c829.id}'], [self.done.id, self.waiting.id])  # другая логистика тоже

    def test_note_with_own_column_leaves_car_column_and_cannot_move(self):
        pickup = Option.objects.get(kind=Option.NOTE, name='Самовывоз')
        S.update_row(self.done, self.manager, {'note': pickup.id})
        cards = self.cards(self.sobrano)
        self.assertEqual(cards[f'car:{self.c829.id}'], [])
        self.assertEqual(cards[f'note:{pickup.id}'], [self.done.id])
        with self.assertRaises(S.Problem):
            S.move_cards(self.sobrano, {f'car:{self.c579.id}': [self.done.id]})

    def test_assembled_after_save_marked_new(self):
        S.save_board(self.sobrano, self.manager)
        S.update_row(self.waiting, self.sklad, {'assembled': True})
        self.sobrano.refresh_from_db()
        view = S.board_view(self.sobrano)
        flags = {c['id']: c['new'] for col in view['columns'] for c in col['cards']}
        self.assertEqual(flags, {self.done.id: False, self.waiting.id: True})

    def test_route_swap_keeps_cards(self):
        S.swap_column(self.sobrano, self.c829, self.c579)
        col = next(c for c in S.board_view(self.sobrano)['columns'] if c['key'] == f'car:{self.c829.id}')
        self.assertEqual(col['replacement']['number'], '579')
        self.assertEqual([c['id'] for c in col['cards']], [self.done.id])
        S.reset_board(self.sobrano)
        col = next(c for c in S.board_view(self.sobrano)['columns'] if c['key'] == f'car:{self.c829.id}')
        self.assertIsNone(col['replacement'])


class Permissions(Base):
    def setUp(self):
        super().setUp()
        self.old = S.create_sheet(self.manager, self.day)
        self.row = S.add_row(self.old, self.manager, {'counterparty': self.firm.id})
        self.current = S.create_sheet(self.manager, self.day + timedelta(days=1))

    def test_warehouse_cannot_create_sheet_or_move_logistics(self):
        self.assertEqual(self.post(self.sklad, '/api/sheets', {'date': '2026-10-25'}).status_code, 403)
        url = f'/api/sheets/{self.current.id}/board/sobrano/move'
        self.assertEqual(self.post(self.sklad, url, {'columns': {'nocar': []}}).status_code, 403)

    def test_warehouse_fills_yesterday_in_archive_but_sees_only_assembly(self):
        res = self.post(self.sklad, f'/api/rows/{self.row.id}', {'changes': {'assembled': True, 'places': 3}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.get(self.sklad, f'/api/sheets/{self.old.id}/board/sobrano').status_code, 403)
        self.assertEqual(self.get(self.manager, f'/api/sheets/{self.old.id}/board/sobrano').status_code, 200)

    def test_only_director_deletes_archive(self):
        self.assertEqual(self.post(self.manager, f'/api/sheets/{self.old.id}/delete').status_code, 403)
        self.assertEqual(self.post(self.director, f'/api/sheets/{self.old.id}/delete').status_code, 200)
        self.assertFalse(Sheet.objects.filter(pk=self.old.id).exists())

    def test_current_sheet_cannot_be_deleted(self):
        self.assertEqual(self.post(self.director, f'/api/sheets/{self.current.id}/delete').status_code, 400)

    def test_staff_managed_by_director_only(self):
        self.assertEqual(self.get(self.manager, '/api/users').status_code, 403)
        res = self.post(self.director, '/api/users/new', {'name': 'Ольга З.', 'username': 'olga.z', 'role': 'manager'})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(User.objects.get(username='olga.z').must_change_password)

    def test_pages_require_login(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self.client.get('/api/bootstrap').status_code, 401)
        self.assertEqual(self.client.get('/api/sheets/current').status_code, 401)
