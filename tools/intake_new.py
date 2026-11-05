# Library Sync standalone export; LicensePending. See release/BUILD.md.
import csv
import re
from library_paths import LIBRARY as LIB, REPORTS
CK_V2 = re.compile('^[A-Za-z][A-Za-z0-9-]*\\d{4}[A-Za-z0-9]*-[A-Za-z0-9]+(CN)?[a-z]?$')
JOURNAL_TYPES = ('journalArticle', 'magazineArticle', 'newspaperArticle')
MEETING = 'MEETING'

def norm(t: str) -> str:
    return re.sub('\\s+', ' ', (t or '').strip().lower())

def acronym_table() -> dict[str, str]:
    table: dict[str, str] = {}
    with open(REPORTS / 'journal_acronyms.csv', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if r['title'] != '(空)' and r['proposed'].strip():
                table[norm(r['title'])] = r['proposed'].strip()
    return table
