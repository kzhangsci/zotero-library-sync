# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import argparse
import configparser
import csv
import datetime as _dt
import hashlib
import os
import re
import shutil
import sqlite3
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from library_paths import REPO, REPORTS, CACHE, DATA, ZOTERO_DATA, LEGACY_DEFAULTS, PATHS
IS_WIN = os.name == 'nt'
DEFAULT_ENDNOTE_DATA = LEGACY_DEFAULTS.get('endnote_data', str(DATA / 'endnote-not-configured'))
DEFAULT_ZOTERO_DATA = str(ZOTERO_DATA)
ZOTERO_PROFILES_INI = str(Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming')) / 'Zotero' / 'Zotero' / 'profiles.ini')
IMPORT_ROOT = LEGACY_DEFAULTS.get('import_root', str(DATA / 'imports'))
JS_DIR = REPO / 'zotero_js'
EN_TAG_PREFIX = '_en:'
EN_LABEL_TAG_PREFIX = '_enlabel:'
EXTRA_RECORD_PREFIX = 'EndNote Record: '
EXTRA_LABEL_PREFIX = 'EndNote Label: '
IMPORT_NOTE_TAG = '_EndnoteXML import'
MISSING_ATTACHMENT_TAG = 'attachment-missing'
SDB_REF_TYPE_NAMES = {0: 'Journal Article', 1: 'Book', 2: 'Thesis', 3: 'Conference Proceedings', 4: 'Personal Communication', 5: 'Newspaper Article', 7: 'Book Section', 10: 'Report', 25: 'Figure', 32: 'Government Document', 33: 'Conference Paper', 34: 'Online Multimedia', 48: 'Dataset'}
ENDNOTE_TO_ZOTERO_TYPE = {'Journal Article': 'journalArticle', 'Thesis': 'thesis', 'Book Section': 'bookSection', 'Book': 'book', 'Conference Proceedings': 'conferencePaper', 'Conference Paper': 'presentation', 'Dataset': 'report', 'Report': 'report', 'Government Document': 'bill', 'Figure': 'artwork', 'Personal Communication': 'letter', 'Newspaper Article': 'newspaperArticle', 'Online Multimedia': 'webpage', 'Web Page': 'webpage', 'Generic': 'document', 'Magazine Article': 'magazineArticle', 'Edited Book': 'book', 'Electronic Article': 'journalArticle', 'Electronic Book': 'book', 'Unpublished Work': 'manuscript', 'Manuscript': 'manuscript', 'Patent': 'patent'}

def win_path(p: str | Path) -> Path:
    s = str(p)
    if IS_WIN:
        m = re.match('^/mnt/([a-zA-Z])/(.*)$', s)
        if m:
            return Path(f'{m.group(1).upper()}:/' + m.group(2))
        return Path(s)
    m = re.match('^([A-Za-z]):[\\\\/](.*)$', s)
    if m:
        return Path('/mnt') / m.group(1).lower() / m.group(2).replace('\\', '/')
    return Path(s)

def add_common_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--dry-run', action='store_true', default=True, help='(default) only print what would be done / written')
    g.add_argument('--apply', action='store_true', help='actually write outputs')
    ap.add_argument('--limit', type=int, default=0, help='process at most N records (0 = all)')

def ts(unix: str | int | None) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(unix), _dt.timezone.utc).strftime('%Y-%m-%d')
    except Exception:
        return '?'

def now_tag() -> str:
    return _dt.datetime.now().strftime('%Y%m%d_%H%M%S')

def sha256(path: Path, bufsize: int=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def zotero_data_dir() -> Path:
    if PATHS.config_file is not None or PATHS.portable:
        return ZOTERO_DATA
    ini = win_path(ZOTERO_PROFILES_INI)
    try:
        cp = configparser.ConfigParser()
        cp.read(ini, encoding='utf-8')
        for sec in cp.sections():
            if cp.has_option(sec, 'Path'):
                prof = ini.parent / cp.get(sec, 'Path') if cp.get(sec, 'IsRelative', fallback='1') == '1' else Path(cp.get(sec, 'Path'))
                prefs = prof / 'prefs.js'
                if prefs.exists():
                    m = re.search('user_pref\\("extensions\\.zotero\\.dataDir",\\s*"(.*?)"\\);', prefs.read_text(encoding='utf-8', errors='replace'))
                    if m:
                        return win_path(m.group(1).replace('\\\\', '\\'))
    except Exception:
        pass
    return win_path(DEFAULT_ZOTERO_DATA)

def copy_sdb_readonly(sdb_path: Path, cache_dir: Path=CACHE) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f'sdb_ro_{now_tag()}.sqlite'
    shutil.copy2(sdb_path, dst)
    return dst

def latest_sdb_copy(cache_dir: Path=CACHE) -> Path:
    copies = sorted(cache_dir.glob('sdb_ro_*.sqlite'))
    if not copies:
        raise FileNotFoundError(f'no sdb copy in {cache_dir}; run tools/endnote_inventory.py first')
    return copies[-1]

def open_ro(sqlite_path: Path) -> sqlite3.Connection:
    uri = 'file:' + sqlite_path.resolve().as_posix() + '?mode=ro'
    return sqlite3.connect(uri, uri=True)

def normalize_doi(s: str) -> str:
    s = (s or '').strip()
    s = re.sub('^(https?://)?(dx\\.)?doi\\.org/', '', s, flags=re.I)
    s = re.sub('^doi\\s*:?\\s*', '', s, flags=re.I)
    return s.strip().lower()

def normalize_title(s: str) -> str:
    s = re.sub('<[^>]+>', '', s or '')
    return re.sub('[^0-9a-z\\u4e00-\\u9fff]+', '', s.lower())

def first_author(author_field: str) -> str:
    for line in re.split('[\\r\\n]+', author_field or ''):
        line = line.strip()
        if line:
            return line
    return ''
_YEAR_RE = re.compile('(?<!\\d)(1[5-9]\\d{2}|20\\d{2})(?!\\d)')

def year_of(d: dict | None=None, date: str | None=None, parsed: str | None=None) -> str:
    if d:
        parsed = parsed or d.get('_parsedDate') or (d.get('meta') or {}).get('parsedDate') or ''
        date = date if date is not None else d.get('date') or ''
    parsed, date = ((parsed or '').strip(), (date or '').strip())
    if len(parsed) >= 4 and parsed[:4].isdigit():
        return parsed[:4]
    m = _YEAR_RE.search(date)
    return m.group(1) if m else ''

def read_refs(conn: sqlite3.Connection) -> dict[int, dict]:
    cols = ('id', 'trash_state', 'reference_type', 'author', 'year', 'title', 'secondary_title', 'label', 'electronic_resource_number', 'url', 'keywords', 'notes', 'research_notes', 'custom_7', 'accession_number', 'added_to_library', 'record_last_updated')
    out = {}
    for row in conn.execute('select ' + ','.join(cols) + ' from refs'):
        d = dict(zip(cols, row))
        d['first_author'] = first_author(d['author'])
        d['doi_norm'] = normalize_doi(d['electronic_resource_number'])
        d['title_norm'] = normalize_title(d['title'])
        d['type_name'] = SDB_REF_TYPE_NAMES.get(d['reference_type'], f"type#{d['reference_type']}")
        out[d['id']] = d
    return out

def read_file_res(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    return [(int(r), p) for r, p in conn.execute('select refs_id, file_path from file_res order by refs_id, file_pos')]

def _xml_text(root: ET.Element, path: str) -> str:
    el = root.find(path)
    return (el.text or '').strip() if el is not None else ''

def read_groups(conn: sqlite3.Connection) -> tuple[list[dict], dict[str, dict]]:
    groups: dict[str, dict] = {}
    for gid, stamp, spec, mem in conn.execute('select group_id, recs_stamp, spec, members from groups order by group_id'):
        root = ET.fromstring(bytes(spec).decode('utf-8'))
        mem = bytes(mem)
        n = len(mem) // 4
        ints = list(struct.unpack('<%dI' % n, mem)) if n else []
        head, body = (ints[:2], ints[2:])
        if len(head) > 1 and head[1] != len(body):
            raise RuntimeError(f'unexpected members header for group {gid}: {head} vs {len(body)} ids')
        rule = _xml_text(root, './/rule')
        groups[_xml_text(root, './ids/id')] = {'gid': gid, 'name': _xml_text(root, './ids/name'), 'rule': rule, 'kind': 'custom' if rule == 'TYPE;3' else 'online_search' if rule == 'TYPE;6' else rule, 'created': ts(_xml_text(root, './/created')), 'modified': ts(_xml_text(root, './/modified')), 'members': body}
    sets: dict[str, dict] = {}
    order: list[str] = []
    for code, sub, val in conn.execute('select code, subcode, value from misc where code in (17, 18) order by code, subcode'):
        root = ET.fromstring(bytes(val).decode('utf-8'))
        if code == 18:
            order = [s.text for s in root.findall('.//set')]
            continue
        sets[_xml_text(root, './ids/id')] = {'subcode': sub, 'name': _xml_text(root, './ids/name'), 'created': ts(_xml_text(root, './/created')), 'member_uuids': [m.text for m in root.findall('.//member')]}
    ordered = [dict(uuid=u, hidden=False, **sets[u]) for u in order if u in sets]
    ordered += [dict(uuid=u, hidden=True, **s) for u, s in sets.items() if u not in order]
    return (ordered, groups)

def custom_groups_with_sets(sets: list[dict], groups: dict[str, dict]) -> list[tuple[str, dict]]:
    out = []
    for s in sets:
        for u in s['member_uuids']:
            g = groups.get(u)
            if g and g['kind'] == 'custom':
                out.append((s['name'], g))
    return out

def load_mapping(path: Path) -> list[dict]:
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def build_plan(sets: list[dict], groups: dict[str, dict], rows: list[dict]) -> tuple[list[dict], list[tuple[str, int]], list[str]]:
    by_gid = {g['gid']: g for g in groups.values()}
    plan: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    skipped: list[tuple[str, int]] = []
    warnings: list[str] = []
    seen_gids = set()
    for row in rows:
        gid = int(row['endnote_gid'])
        g = by_gid.get(gid)
        if not g:
            warnings.append(f"mapping row for unknown group gid={gid} ({row.get('set_name')}/{row.get('group_name')})")
            continue
        seen_gids.add(gid)
        src = f"{row['set_name']}/{row['group_name']}"
        action = (row.get('action') or 'keep').strip().lower()
        if action == 'skip':
            skipped.append((src, len(g['members'])))
            continue
        if action not in ('keep', 'rename', 'merge'):
            warnings.append(f'unknown action {action!r} for {src}, treated as keep')
        parent = (row.get('target_parent') or row['set_name']).strip()
        name = (row.get('target_name') or row['group_name']).strip()
        key = (parent, name)
        if key not in plan:
            plan[key] = {'set': parent, 'name': name, 'recs': set(), 'sources': []}
            order.append(key)
        plan[key]['recs'].update(g['members'])
        plan[key]['sources'].append(src)
    for s_name, g in custom_groups_with_sets(sets, groups):
        if g['gid'] not in seen_gids:
            warnings.append(f"custom group not in mapping (will NOT be migrated): {s_name}/{g['name']} gid={g['gid']} n={len(g['members'])}")
    out: list[dict] = []
    idx: dict[str, int] = {}
    for key in order:
        p = plan[key]
        if p['set'] not in idx:
            idx[p['set']] = len(out)
            out.append({'set': p['set'], 'groups': []})
        out[idx[p['set']]]['groups'].append({'name': p['name'], 'recs': sorted(p['recs']), 'sources': p['sources']})
    return (out, skipped, warnings)

def plan_membership(plan: list[dict]) -> dict[int, list[str]]:
    memb: dict[int, list[str]] = {}
    for s in plan:
        for g in s['groups']:
            for r in g['recs']:
                memb.setdefault(r, []).append(f"{s['set']}/{g['name']}")
    return memb

def el_text(el: ET.Element | None) -> str:
    if el is None:
        return ''
    return ''.join(el.itertext()).strip()

def iter_xml_records(xml_path: Path):
    tree = ET.parse(xml_path)
    for rec in tree.getroot().iter('record'):
        yield rec

def xml_record_summary(rec: ET.Element) -> dict:
    rt = rec.find('ref-type')
    return {'rec_number': int(el_text(rec.find('rec-number')) or 0), 'ref_type': rt.get('name') if rt is not None else '', 'title': el_text(rec.find('./titles/title')), 'year': el_text(rec.find('./dates/year')), 'first_author': el_text(rec.find('./contributors/authors/author')), 'doi': el_text(rec.find('electronic-resource-num')), 'label': el_text(rec.find('label')), 'pdf_urls': [el_text(u) for u in rec.findall('./urls/pdf-urls/url')], 'related_urls': [el_text(u) for u in rec.findall('./urls/related-urls/url')], 'keywords': [el_text(k) for k in rec.findall('./keywords/keyword')], 'has_notes': rec.find('notes') is not None, 'has_research_notes': rec.find('research-notes') is not None}

def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        fieldnames = fieldnames or []
    fieldnames = fieldnames or list(rows[0].keys())
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
