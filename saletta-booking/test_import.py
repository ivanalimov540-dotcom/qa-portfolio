import io
import json

from django.core.files.uploadedfile import SimpleUploadedFile

from booking.models import Guest, Role

from .base import Base


def csv_file(text, name='gosti.csv', encoding='utf-8'):
    return SimpleUploadedFile(name, text.encode(encoding), content_type='text/csv')


def xlsx_file(rows, name='gosti.xlsx'):
    from openpyxl import Workbook
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile(name, buf.getvalue())


RESTOPLACE_LIKE = """ФИО;Телефон;Email;Количество визитов;Последний визит;Комментарий;Статус
Анна Смирнова;+7 (900) 123-45-67;anna@example.com;12;14.09.2026;Любит стол у окна;VIP
Игорь Петров;8 912 765 43 21;;3;02.08.2026;;
Без телефона;;;5;;;
Анна Смирнова;79001234567;;2;20.09.2026;;
Странный номер;12-34;;1;;;
"""


class GuestImport(Base):
    def upload(self, f, op='preview', mapping=None, client=None, expect=200):
        data = {'file': f, 'op': op}
        if mapping is not None:
            data['mapping'] = json.dumps(mapping)
        res = (client or self.c).post('/api/guests/import', data)
        self.assertEqual(res.status_code, expect, res.content.decode())
        return res.json()

    def test_preview_guesses_columns(self):
        pre = self.upload(csv_file(RESTOPLACE_LIKE))
        self.assertEqual(pre['total'], 5)
        self.assertEqual(pre['guess'], {'name': 0, 'phone': 1, 'visits': 3, 'last': 4, 'note': 5})
        self.assertEqual(pre['sample'][0][0], 'Анна Смирнова')

    def test_run_merges_duplicates_and_skips_rows_without_phone(self):
        mapping = {'name': 0, 'phone': 1, 'visits': 3, 'last': 4, 'note': 5}
        res = self.upload(csv_file(RESTOPLACE_LIKE), 'run', mapping)
        self.assertEqual((res['created'], res['updated'], res['skipped']), (2, 0, 2))
        self.assertEqual([r['row'] for r in res['skippedRows']], [4, 6])
        anna = Guest.objects.get(phone_key='9001234567')
        self.assertEqual((anna.name, anna.visits_before, str(anna.last_visit_before), anna.note, anna.phone),
                         ('Анна Смирнова', 14, '2026-09-20', 'Любит стол у окна', '+7 900 123-45-67'))
        # В списке гостей видны визиты до системы
        listed = {g['name']: g for g in self.get('/api/guests')['guests']}
        self.assertEqual(listed['Игорь Петров']['visits'], 3)
        # Повторная загрузка того же файла не удваивает историю
        again = self.upload(csv_file(RESTOPLACE_LIKE), 'run', mapping)
        self.assertEqual((again['created'], again['updated']), (0, 2))
        self.assertEqual(Guest.objects.get(phone_key='9001234567').visits_before, 14)

    def test_existing_guest_keeps_name_and_note(self):
        self.book(name='Анна С.', phone='+7 900 123-45-67', note='')
        guest = Guest.objects.get(phone_key='9001234567')
        guest.note = 'Аллергия на орехи'
        guest.save()
        self.upload(csv_file(RESTOPLACE_LIKE), 'run', {'name': 0, 'phone': 1, 'note': 5, 'vip': 6})
        guest.refresh_from_db()
        self.assertEqual((guest.name, guest.note, guest.vip), ('Анна С.', 'Аллергия на орехи', True))

    def test_windows_encoding_and_comma_separator(self):
        text = 'Имя,Телефон,Неявки,Чёрный список\nОлег,+79005556677,4,да\n'
        pre = self.upload(csv_file(text, encoding='cp1251'))
        self.assertEqual(pre['guess'], {'name': 0, 'phone': 1, 'noshows': 2, 'black': 3})
        self.upload(csv_file(text, encoding='cp1251'), 'run', pre['guess'])
        oleg = Guest.objects.get(phone_key='9005556677')
        self.assertEqual((oleg.noshows_before, oleg.blacklisted), (4, True))

    def test_excel_file(self):
        f = lambda: xlsx_file([['Гость', 'Мобильный', 'Визиты'], ['Мария', 79007778899, 7], ['Пётр', '+7 900 111 00 00', None]])  # noqa: E731
        pre = self.upload(f())
        self.assertEqual((pre['guess'], pre['sample'][0]), ({'name': 0, 'phone': 1, 'visits': 2}, ['Мария', '79007778899', '7']))
        res = self.upload(f(), 'run', pre['guess'])
        self.assertEqual(res['created'], 2)
        self.assertEqual(Guest.objects.get(phone_key='9007778899').visits_before, 7)

    def test_bad_files_and_mapping(self):
        self.assertIn('xls', self.upload(SimpleUploadedFile('old.xls', b'\xd0\xcf\x11\xe0junk'), expect=400)['error'])
        self.assertIn('Excel', self.upload(SimpleUploadedFile('broken.xlsx', b'PK\x03\x04broken'), expect=400)['error'])
        self.assertIn('строк', self.upload(csv_file('Имя;Телефон\n'), expect=400)['error'])
        self.assertIn('телефон', self.upload(csv_file(RESTOPLACE_LIKE), 'run', {'name': 0}, expect=400)['error'])
        self.upload(csv_file(RESTOPLACE_LIKE), 'run', {'phone': 99}, expect=400)

    def test_only_managers_with_whole_network(self):
        admin = self.login(self.member('adm', Role.ADMIN))
        self.upload(csv_file(RESTOPLACE_LIKE), client=admin, expect=403)
        from booking.models import Membership
        manager = self.member('man', Role.MANAGER)
        m = Membership.objects.get(user=manager)
        m.all_restaurants = False
        m.save()
        m.restaurants.add(self.r)
        self.upload(csv_file(RESTOPLACE_LIKE), client=self.login(manager), expect=403)

    def test_sample_file(self):
        res = self.c.get('/api/guests/import/sample.csv')
        self.assertEqual(res.status_code, 200)
        self.assertIn('Телефон', res.content.decode('utf-8-sig'))


class ImportEdgeCases(GuestImport):
    def test_row_numbers_foreign_numbers_and_dates(self):
        text = ('Имя;Телефон;Последний визит\n;;\nОльга;;\nЯнош;+375 29 123-45-67;\nПётр;8-900-123-45-67 доб. 12;\n'
                'Вера;+7 900 222-11-00;2026-09-14T19:30:00\nЛев;+7 900 222-11-01;46279\nИра;+7 900 222-11-02;позавчера\n')
        res = self.upload(csv_file(text), 'run', {'name': 0, 'phone': 1, 'last': 2})
        self.assertEqual([(x['row'], x['why'][:19]) for x in res['skippedRows']],
                         [(3, 'нет телефона'), (4, 'не российский номер'), (5, 'не российский номер')])
        self.assertEqual(res['created'], 3)
        self.assertEqual(str(Guest.objects.get(phone_key='9002221100').last_visit_before), '2026-09-14')
        self.assertEqual(str(Guest.objects.get(phone_key='9002221101').last_visit_before), '2026-09-14')  # число 46279 из Excel
        self.assertEqual([x['row'] for x in res['warningRows']], [8])

    def test_wide_and_broken_files_are_handled(self):
        wide = 'Имя;Телефон' + ';x' * 5000 + '\nАнна;+79001112233\n'
        pre = self.upload(csv_file(wide))
        self.assertLessEqual(len(pre['columns']), 60)
        broken = csv_file('Имя;Телефон\n"' + 'a' * 140000 + '";+79001112233\n')
        self.assertIn('повреждён', self.upload(broken, expect=400)['error'])


class PhoneContacts(Base):
    def test_iphone_vcard(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        text = ('BEGIN:VCARD\r\nVERSION:3.0\r\nN:Петров;Иван;;;\r\nFN:Иван Петров\r\nitem1.TEL;type=pref:+7 495 111-22-33\r\n'
                'TEL;type=CELL;type=VOICE:+7 (916) 555-12-34\r\nNOTE:VIP\\, любит \r\n окно\r\nEND:VCARD\r\n'
                'BEGIN:VCARD\r\nVERSION:2.1\r\nN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D0=90=D0=BD=D0=BD=D0=B0;;;;\r\nTEL;CELL:89001112233\r\nEND:VCARD\r\n')
        res = self.c.post('/api/guests/import', {'op': 'preview', 'file': SimpleUploadedFile('contacts.vcf', text.encode())}).json()
        self.assertEqual(res['sample'], [['Иван Петров', '+7 (916) 555-12-34', 'VIP, любит окно'], ['Анна', '89001112233', '']])
        self.assertEqual(res['guess'], {'name': 0, 'phone': 1, 'note': 2})
