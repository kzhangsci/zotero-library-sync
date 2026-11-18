# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
import csv
import re
from pathlib import Path
from library_paths import LIBRARY as LIB, REPORTS
CK_V2 = re.compile('^[A-Za-z][A-Za-z0-9-]*\\d{4}[A-Za-z0-9]*-[A-Za-z0-9]+(CN)?[a-z]?$')
JOURNAL_TYPES = ('journalArticle', 'magazineArticle', 'newspaperArticle')
MEETING = 'MEETING'

def norm(t: str) -> str:
    return re.sub('\\s+', ' ', (t or '').strip().lower())
BASE_TABLE = Path(__file__).resolve().parents[1] / 'release' / 'journal_acronyms.base.csv'
ACRONYM = re.compile('[A-Za-z0-9]{1,12}\\Z')

def _acronym_rows(path: Path, source: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        fields = [x.strip() for x in reader.fieldnames or []]
        if 'title' not in fields or 'proposed' not in fields:
            raise RuntimeError(f'{path.name} 表头必须包含 title 与 proposed 列')
        for line_no, r in enumerate(reader, start=2):
            title, proposed = ((r.get('title') or '').strip(), (r.get('proposed') or '').strip())
            if not title or title == '(空)':
                continue
            if title.startswith(('=', '+', '-', '@', '\t')) or any((ord(c) < 32 for c in title)) or len(title) > 300 or ('\n' in title):
                raise RuntimeError(f'{path.name} 第 {line_no} 行刊名含公式注入/控制字符，已拒绝整表读取')
            if not proposed:
                continue
            if not ACRONYM.fullmatch(proposed):
                raise RuntimeError(f'{path.name} 第 {line_no} 行缩写 {proposed!r} 含非法字符（仅字母数字，1–12 位）')
            rows.append((title, proposed, source))
    return rows

def acronym_table() -> dict[str, str]:
    table: dict[str, str] = {}
    for path, source in ((BASE_TABLE, 'base'), (REPORTS / 'journal_acronyms.csv', 'user')):
        if not path.is_file():
            continue
        seen: dict[str, str] = {}
        for title, proposed, origin in _acronym_rows(path, source):
            key = norm(title)
            if key in seen and seen[key] != proposed:
                raise RuntimeError(f'{path.name} 中刊名「{title}」同时映射到 {seen[key]} 与 {proposed}；请先解决冲突')
            seen[key] = proposed
        table.update(seen)
    return table
