"""Pure export and authorization helpers; no Telegram credentials here."""
from dataclasses import dataclass
from pathlib import Path
import re
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE


@dataclass
class Member:
    name: str
    username: str
    user_id: int
    role: str
    title: str = ''


def authorized(status):
    return status in ('creator', 'administrator')


def target(text):
    text = text.strip()
    if re.fullmatch(r'-[0-9]{1,20}', text):
        return int(text)
    match = re.fullmatch(r'(?:https://t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})/?', text)
    if match:
        return '@' + match[1]
    raise ValueError('Отправьте @username, https://t.me/username или отрицательный ID чата.')


def clean(value):
    return ILLEGAL_CHARACTERS_RE.sub('', str(value))[:32767]


def line(m):
    # Prevent embedded newlines or tabs from impersonating another row.
    return ' | '.join(' '.join(str(x).split()) for x in
                      (m.name, '@' + m.username if m.username else '—', m.user_id, m.role, m.title or '—'))


def clean_needle(text):
    folded = ''.join(ch for ch in text.strip() if ch not in '\ufe0e\ufe0f').strip()
    if not folded or len(folded) > 32:
        return ''
    return folded


def filter_lines(members, needle):
    """Names containing the emoji, as «ник - должность», plus both counts."""
    needle = clean_needle(needle)
    matched = [m for m in members if needle and needle in ''.join(ch for ch in m.name if ch not in '\ufe0e\ufe0f')]
    matched.sort(key=lambda m: (' '.join(str(m.name).split()).casefold(), m.user_id))
    lines = []
    for m in matched:
        nick = ' '.join(str(m.name).split()) or '—'
        post = ' '.join(str(m.title).split()) if str(m.title).strip() else '—'
        lines.append(f'{nick} - {post}')
    return lines, len(matched), len(members) - len(matched)


def chunks(text, limit=3500):
    # UTF-16 units: Telegram counts astral characters as two units.
    current, size = '', 0
    for char in text:
        width = 2 if ord(char) > 0xffff else 1
        if size + width > limit:
            yield current
            current, size = '', 0
        current += char
        size += width
    if current:
        yield current


def write_files(directory: Path, rows, summary):
    txt, xlsx = directory / 'participants.txt', directory / 'participants.xlsx'
    txt.write_text(summary + '\n\nИмя | username | Telegram ID | роль | подпись\n' +
                   '\n'.join(line(m) for m in rows), encoding='utf-8-sig')
    wb = Workbook()
    ws = wb.active
    ws.title = 'Участники'
    ws.append(['Имя', 'username', 'Telegram ID', 'Роль', 'Подпись / должность'])
    for m in rows:
        ws.append([clean(m.name), clean(m.username), str(m.user_id), m.role, clean(m.title)])
        for cell in ws[ws.max_row]:
            cell.data_type = 's'
            cell.number_format = '@'
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for col, width in zip('ABCDE', (36, 25, 24, 16, 36)):
        ws.column_dimensions[col].width = width
    info = wb.create_sheet('О результате')
    for index, text in enumerate(summary.splitlines(), 1):
        c = info.cell(index, 1, clean(text))
        c.data_type = 's'
    info.column_dimensions['A'].width = 110
    wb.save(xlsx)
    return txt, xlsx
