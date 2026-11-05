# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from attachments_audit import RX_MANU, RX_PRE, RX_SUPP, RX_TRANS, role_of, scan_pdf
from attachments_normalize_prep import EXPLICIT_RENAMES, build_actions, strip_annotations
from citekey_reformat_prep import PINS
from common import REPORTS, sha256, year_of, zotero_data_dir
from intake_new import CK_V2, JOURNAL_TYPES, LIB, MEETING, acronym_table, norm
if TYPE_CHECKING:
    from zotero_local import Client
BBT_WAIT_SECONDS = 30.0
BBT_POLL_SECONDS = 0.5
STORED = {'imported_file', 'imported_url'}
SECONDARY = (('supplement', RX_SUPP), ('preprint', RX_PRE), ('manuscript', RX_MANU), ('translation', RX_TRANS))

class IntakeBlocked(RuntimeError):
    pass

def _name(value: str) -> str:
    if not value or value in {'.', '..'} or value.endswith(('.', ' ')) or re.search('[<>:"/\\\\|?*\\x00-\\x1f]', value):
        raise IntakeBlocked(f'unsafe filename/key: {value!r}')
    return value

def _item(raw: dict, key: str | None=None) -> dict:
    if not isinstance(raw, dict):
        raise IntakeBlocked('API did not return an item object')
    data = copy.deepcopy(raw.get('data', raw))
    actual = raw.get('key') or data.get('key')
    version = raw.get('version', data.get('version'))
    if not actual or (key and actual != key) or data.get('deleted'):
        raise IntakeBlocked(f'missing, deleted or wrong item: {key or actual}')
    _name(actual)
    if not isinstance(version, int) or isinstance(version, bool):
        raise IntakeBlocked(f'missing item version: {actual}')
    return {'key': actual, 'version': version, 'data': data, 'meta': copy.deepcopy(raw.get('meta', {}))}

def _get(client, key: str) -> dict:
    return _item(client.get(f'items/{_name(key)}'), key)

def _children(client, parent: dict) -> list[dict]:
    bundle = client.parent_children(parent['key'])
    if not isinstance(bundle, dict):
        raise IntakeBlocked(f'invalid parent/children snapshot: {parent['key']}')
    current = _item(bundle.get('parent'), parent['key'])
    if current['version'] != parent['version'] or current['data'] != parent['data']:
        raise IntakeBlocked(f'parent changed while reading children: {parent['key']}')
    version = bundle.get('library_version')
    if not isinstance(version, int) or isinstance(version, bool) or current['version'] > version:
        raise IntakeBlocked(f'missing/inconsistent children snapshot version: {parent['key']}')
    raw = bundle.get('children')
    if not isinstance(raw, list):
        raise IntakeBlocked(f'children response is not a list: {parent['key']}')
    children = [_item(x) for x in raw]
    if any((x['version'] > version for x in children)):
        raise IntakeBlocked(f'child newer than snapshot: {parent['key']}')
    expected = parent.get('meta', {}).get('numChildren', parent['data'].get('numChildren'))
    regular = parent['data'].get('itemType') not in {'attachment', 'annotation', 'note'}
    if regular and expected is not None and (len(children) != int(expected)):
        raise IntakeBlocked(f'incomplete children: {parent['key']} {len(children)} != {expected}')
    if len({x['key'] for x in children}) != len(children):
        raise IntakeBlocked(f'duplicate child keys: {parent['key']}')
    if any((x['data'].get('parentItem') != parent['key'] for x in children)):
        raise IntakeBlocked(f'unexpected child parent: {parent['key']}')
    return children

def _pin(parent: dict) -> tuple[bool, str]:
    d, key = (parent['data'], parent['key'])
    match = re.search('(?im)^\\s*citation\\s*key\\s*:\\s*(.*?)\\s*$', d.get('extra', ''))
    if key in PINS:
        return (True, PINS[key])
    if match:
        return (True, match.group(1))
    for part in (d, parent.get('meta', {}), d.get('bbt', {}), parent.get('meta', {}).get('bbt', {})):
        if part.get('pinned') is True or part.get('citationKeyPinned') is True:
            return (True, d.get('citationKey', ''))
    return (False, '')

def _extra_acronym(extra: str, acronym: str) -> str:
    lines = extra.splitlines()
    hits = [i for i, line in enumerate(lines) if re.match('(?i)^\\s*tex\\.jabbr\\s*:', line)]
    if hits:
        if len(hits) > 1:
            raise IntakeBlocked('ambiguous duplicate tex.jabbr fields')
        lines[hits[0]] = f'tex.jabbr: {acronym}'
    else:
        lines.append(f'tex.jabbr: {acronym}')
    return '\n'.join(lines)

def _proper_key(parent: dict, metadata: dict) -> bool:
    ck = parent['data'].get('citationKey') or ''
    if metadata['pinned']:
        return bool(ck) and ck == metadata['pin_value']
    if not CK_V2.fullmatch(ck):
        return False
    acronym = metadata.get('acronym')
    year = year_of(parent['data'] | {'_parsedDate': parent['meta'].get('parsedDate', '')})
    if year and (not re.search(re.escape(year) + '[A-Za-z0-9]*-', ck)):
        return False
    return not acronym or bool(re.search('\\d{4}' + re.escape(acronym) + '-', ck))

def _metadata(parent: dict) -> dict:
    d = parent['data']
    changes, acronym = ({}, '')
    pinned, pin_value = _pin(parent)
    if pinned and (not pin_value or d.get('citationKey') != pin_value):
        raise IntakeBlocked('missing or changed pinned citation key; preserve pin for manual repair')
    kind = d.get('itemType')
    if kind in JOURNAL_TYPES:
        title = d.get('publicationTitle') or ''
        acronym = acronym_table().get(norm(title), '')
        if not title or not acronym:
            raise IntakeBlocked(f'missing/unmapped publicationTitle: {title!r}')
        if kind != 'newspaperArticle':
            if (d.get('journalAbbreviation') or '').strip() != acronym:
                changes['journalAbbreviation'] = acronym
        else:
            extra = _extra_acronym(d.get('extra') or '', acronym)
            if extra != (d.get('extra') or ''):
                changes['extra'] = extra
    elif kind == 'conferencePaper':
        acronym = MEETING
        extra = _extra_acronym(d.get('extra') or '', acronym)
        if extra != (d.get('extra') or ''):
            changes['extra'] = extra
    if acronym and kind in JOURNAL_TYPES and (kind != 'newspaperArticle') and re.search('(?im)^\\s*tex\\.jabbr\\s*:', d.get('extra') or ''):
        extra = _extra_acronym(d.get('extra') or '', acronym)
        if extra != d.get('extra'):
            changes['extra'] = extra
    result = {'changes': changes, 'pinned': pinned, 'pin_value': pin_value, 'acronym': acronym, 'refresh': False}
    result['refresh'] = not pinned and (bool(changes) or not _proper_key(parent, result))
    if result['refresh']:
        changes['citationKey'] = ''
    result['previous'] = {f: copy.deepcopy(d.get(f, '')) for f in changes}
    return result

def _path(storage: Path, attachment: dict) -> Path:
    d = attachment['data']
    if d.get('linkMode') not in STORED:
        raise IntakeBlocked(f'unsupported linked attachment: {attachment['key']}')
    directory = storage / _name(attachment['key'])
    path = directory / _name(d.get('filename') or '')
    if not path.resolve().is_relative_to(storage.resolve()):
        raise IntakeBlocked(f'attachment escapes storage: {attachment['key']}')
    if not path.is_file():
        raise IntakeBlocked(f'missing attachment file: {path}')
    return path

def _infer(attachment: dict, parent: dict, n_pdfs: int, hints: dict) -> tuple[str, str]:
    key, d = (attachment['key'], attachment['data'])
    ck, filename = (parent['data'].get('citationKey') or '', d.get('filename') or '')
    explicit = EXPLICIT_RENAMES.get(parent['key'], {}).get(key)
    if explicit:
        role, template = explicit
        return (role, template.replace('{ck}', ''))
    if key in hints:
        return (hints[key]['role'], hints[key]['suffix'])
    archived = re.search('(_superseded_[A-Z0-9]{8}\\.pdf)$', filename)
    if archived:
        return ('archive', archived.group(1))
    role, _, suggestion, _ = role_of(filename, ck, n_pdfs)
    if ck and filename.startswith(ck) and (role != 'other'):
        return (role, filename[len(ck):])
    evidence = filename + ' ' + (d.get('title') or '')
    for secondary, rx in SECONDARY:
        if rx.search(evidence):
            return (secondary, f'_{secondary}{Path(filename).suffix.lower()}')
    if re.search('(?i)(?:^|[_\\s-])annotated\\d*(?:\\.pdf|$|\\s)', evidence):
        return ('annotated', '_annotated.pdf')
    if role == 'other' and suggestion == 'primary' and (n_pdfs == 1):
        return ('primary', '.pdf')
    if role != 'other':
        return (role, '')
    raise IntakeBlocked(f'ambiguous attachment role: {key} {filename}')

def _redo(ck: str, path: Path | None, parent=None) -> bool:
    if not ck or path is None:
        return False
    folder = LIB / 'papers' / _name(ck)
    sources = [p for p in (folder / f'{ck}_EN.md', folder / f'{ck}_MD.md') if p.is_file()]
    if not sources:
        return False
    if parent:
        from library_source import find_previous_source
        previous = find_previous_source(LIB, REPORTS / '_cache' / 'library_sync', parent)
        if previous and previous.get('blocked'):
            raise IntakeBlocked('旧来源不能可靠核实：' + '; '.join(previous['blocked']))
        if previous and previous.get('source_sha256'):
            return previous['source_sha256'] != sha256(path)
    return any((p.stat().st_mtime_ns < path.stat().st_mtime_ns for p in sources))

def _requires_translation(parent: dict) -> bool:
    language = (parent['data'].get('language') or '').strip().lower()
    return not (language.startswith('zh') or language in {'chinese', '中文', '汉语', 'cn'})

def _make_plan(client, key: str, swap: bool, hints: dict | None=None, cache: dict | None=None, *, allow_redo: bool=False) -> dict:
    plan = {'key': key, 'citekey': '', 'title': '', 'parent': {}, 'children': [], 'annotations': {}, 'metadata': {}, 'actions': [], 'blocked': [], 'warnings': [], 'hashes': {}, 'versions': {}, 'roles': {}, 'rows': [], 'swap': swap, 'primary_pdf': '', 'primary_key': '', 'source_sha256': '', 'redo_required': False, 'require_translation': True}
    cache = cache or {}
    try:
        parent = _get(client, key)
        plan['parent'] = parent
        if parent['data'].get('itemType') in {None, 'attachment', 'annotation', 'note'}:
            raise IntakeBlocked('selected item must be a regular parent')
        ck = parent['data'].get('citationKey') or ''
        plan.update(citekey=ck, title=parent['data'].get('title') or '', require_translation=_requires_translation(parent))
        metadata = plan['metadata'] = _metadata(parent)
        path_ck = ck
        if ck:
            try:
                _name(ck)
            except IntakeBlocked:
                if not metadata['refresh']:
                    raise
                path_ck = ''
        if not allow_redo and metadata['refresh'] and path_ck and (LIB / 'papers' / path_ck).exists():
            raise IntakeBlocked('existing Library folder needs rekey; controller/manual migration required')
        children = plan['children'] = _children(client, parent)
        plan['versions'][key] = parent['version']
        storage = zotero_data_dir() / 'storage'
        plan['storage'] = str(storage)
        attachments = [c for c in children if c['data'].get('itemType') == 'attachment']
        n_pdfs = sum((Path(c['data'].get('filename') or '').suffix.lower() == '.pdf' for c in attachments))
        for child in children:
            plan['versions'][child['key']] = child['version']
        for at in attachments:
            ak, d = (at['key'], at['data'])
            anns = _children(client, at)
            if any((x['data'].get('itemType') != 'annotation' for x in anns)):
                raise IntakeBlocked(f'unexpected attachment child: {ak}')
            plan['annotations'][ak] = anns
            plan['versions'].update({x['key']: x['version'] for x in anns})
            path = _path(storage, at)
            role, suffix = _infer(at, parent, n_pdfs, hints or {})
            plan['roles'][ak] = {'role': role, 'suffix': suffix}
            digest = sha256(path)
            plan['hashes'][ak] = digest
            scan = {}
            if path.suffix.lower() == '.pdf':
                previous = cache.get(ak, {})
                scan = copy.deepcopy(previous['scan']) if previous.get('hash') == digest else scan_pdf(str(path), max_draw_pages=0)
                if scan.get('err') or scan.get('encrypted') or (not scan.get('pages')):
                    raise IntakeBlocked(f'unreadable/encrypted PDF: {ak}: {scan.get('err')}')
                if '?err' in scan.get('embed_all_types', ''):
                    raise IntakeBlocked(f'incomplete annotation scan: {ak}')
            want = path_ck + suffix if path_ck and suffix else d['filename']
            row = {'item_key': key, 'citekey': ck, 'att_key': ak, 'filename': d['filename'], 'ext': path.suffix.lower(), 'exists': True, 'size_bytes': path.stat().st_size, 'role': role if d['filename'] == want else 'other', 'role_basis': 'targeted role inference', 'suggested_role': role, 'suggested_name': want if d['filename'] != want else '', 'item_tags': ','.join((t['tag'] for t in parent['data'].get('tags', []))), 'zot_annots': len(anns), 'embed_markup': 0, 'embed_types': '', 'flat_marks': 0, 'path': str(path), 'scan': scan, **scan}
            plan['rows'].append(row)
        pdfs = [r for r in plan['rows'] if r['ext'] == '.pdf']
        if not pdfs:
            raise IntakeBlocked('no PDF; a clean primary is required')
        by_role = {r: [x for x in pdfs if plan['roles'][x['att_key']]['role'] == r] for r in ('primary', 'annotated')}
        primary, annotated = (by_role['primary'], by_role['annotated'])
        if len(primary) > 1 or len(annotated) > 1:
            raise IntakeBlocked('duplicate primary/annotated PDFs; manual review, nothing deleted')
        if not primary and (not (swap and len(annotated) == 1)):
            raise IntakeBlocked('missing primary PDF; secondary PDFs cannot be selected as paper')
        source = (primary or annotated)[0]
        from library_source import inspect_pdf
        pdf_check = inspect_pdf(source['path'], parent)
        plan['pdf_check'] = pdf_check
        plan['warnings'].extend(pdf_check['warnings'])
        if pdf_check['blocked']:
            raise IntakeBlocked('PDF 完整性检查未通过：' + '; '.join(pdf_check['blocked']))
        plan.update(primary_pdf=source['path'] if primary else '', primary_key=source['att_key'] if primary else '', source_sha256=plan['hashes'][source['att_key']])
        if source.get('flat_marks') and (not source['embed_markup']):
            plan['warnings'].append('suspected flattened marks cannot be removed reliably')
            raise IntakeBlocked('suspected flattened primary: manual review required even with swap')
        if source.get('flat_marks') and source['embed_markup']:
            plan['warnings'].append('embedded annotation appearances can look flattened; stripped copy must pass a fresh scan before import')
        if source['embed_markup'] and (not swap):
            raise IntakeBlocked('embedded annotations in primary; explicit swap=True required')
        if primary and source['embed_markup'] and annotated:
            raise IntakeBlocked('swap would create duplicate annotated copies; manual review required')
        if primary and source['zot_annots'] and (not source['embed_markup']):
            if getattr(client, 'supports_annotation_reparent', False) is not True:
                raise IntakeBlocked('annotation parentItem PATCH capability unconfirmed')
        targets = [ck + v['suffix'] for v in plan['roles'].values() if v['suffix']]
        if len(targets) != len(set((x.casefold() for x in targets))):
            raise IntakeBlocked('ambiguous duplicate role filenames; explicit attachment decisions required')
        if path_ck and (not metadata['refresh']):
            actions, _, skipped, _ = build_actions(copy.deepcopy(plan['rows']), storage, no_swap=not swap, move_annotations=True, make_clean=False, clean_from_annotated=swap, explicit={})
            if skipped:
                raise IntakeBlocked('; '.join(skipped))
            plan['actions'] = actions
        plan['actions_deferred'] = metadata['refresh']
        plan['redo_required'] = _redo(path_ck, Path(source['path']), parent)
        if plan['redo_required'] and (not allow_redo):
            raise IntakeBlocked('redo_required: 正本与已处理来源不同，或旧来源不能确认；请选择文献并勾选“重新检测 / 更换全文 PDF”预演')
    except Exception as exc:
        plan['blocked'].append(f'{type(exc).__name__}: {exc}')
    return plan

def plan_item(client: Client, key: str, *, swap: bool=False) -> dict:
    return _make_plan(client, key, swap)

def _scan_cache(plan: dict) -> dict:
    return {r['att_key']: {'hash': plan['hashes'].get(r['att_key']), 'scan': r['scan']} for r in plan['rows']}

def _assert_snapshot(old: dict, new: dict) -> None:
    if new['blocked']:
        raise IntakeBlocked('; '.join(new['blocked']))
    for field in ('versions', 'hashes', 'roles', 'metadata'):
        if old[field] != new[field]:
            raise IntakeBlocked(f'stale plan: {field} changed; re-plan explicitly')
    if old['parent'] != new['parent'] or old['children'] != new['children'] or old['annotations'] != new['annotations']:
        raise IntakeBlocked('stale plan: item content/children changed')

def _assert_bbt_children(old: dict, new: dict) -> list[dict]:
    before = {x['key']: x for x in old['children']}
    after = {x['key']: x for x in new['children']}
    if before.keys() != after.keys() or old['hashes'] != new['hashes'] or old['roles'] != new['roles'] or (old['annotations'] != new['annotations']):
        raise IntakeBlocked('children changed while waiting for BBT: keys/content/roles/annotations')
    changes = []
    for key, prior in before.items():
        current = after[key]
        if prior == current:
            continue
        a, b = (prior['data'], current['data'])
        if a.get('itemType') != 'attachment' or current['version'] < prior['version']:
            raise IntakeBlocked('children changed while waiting for BBT: non-attachment/version')
        suffix = old['roles'].get(key, {}).get('suffix', '')
        allowed_names = {a.get('filename')}
        if suffix:
            allowed_names.add(new['citekey'] + suffix)
        if b.get('filename') not in allowed_names:
            raise IntakeBlocked('children changed while waiting for BBT: unexpected filename')
        ignored = {'version', 'filename'}
        if {k: v for k, v in a.items() if k not in ignored} != {k: v for k, v in b.items() if k not in ignored} or prior['meta'] != current['meta']:
            raise IntakeBlocked('children changed while waiting for BBT: unrelated attachment fields')
        changes.append({'key': key, 'old_name': a.get('filename'), 'new_name': b.get('filename'), 'old_version': prior['version'], 'new_version': current['version']})
    return changes

class _Journal:

    def __init__(self, key: str):
        directory = REPORTS / '_cache' / 'library_intake'
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = directory / f'{_name(key)}.lock'
        self.path = directory / f'{key}_{uuid.uuid4().hex}.jsonl'

    def __enter__(self):
        self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
        os.write(self.fd, str(self.path).encode('utf-8'))
        os.fsync(self.fd)
        return self

    def write(self, event: str, **data):
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'event': event, **data}, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def __exit__(self, *_):
        os.close(self.fd)
        self.lock.unlink()

def _wait_key(client, key: str, metadata: dict) -> dict:
    deadline = time.monotonic() + min(30.0, BBT_WAIT_SECONDS)
    while True:
        timeout = getattr(client, 'timeout', None)
        remaining = max(0.001, deadline - time.monotonic())
        bounded = isinstance(timeout, (tuple, list)) and len(timeout) == 2
        if bounded:
            client.timeout = tuple((min(float(t), remaining / 2) for t in timeout))
        try:
            parent = _get(client, key)
        finally:
            if bounded:
                client.timeout = timeout
        for field, value in metadata['changes'].items():
            if field != 'citationKey' and parent['data'].get(field, '') != value:
                raise IntakeBlocked(f'metadata readback changed: {field}')
        if _proper_key(parent, metadata):
            return parent
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IntakeBlocked('BBT did not produce a proper v2 citation key within 30 seconds')
        time.sleep(min(BBT_POLL_SECONDS, remaining))

def _checked_attachment(client, key: str, parent: str, digest: str) -> tuple[dict, Path]:
    item = _get(client, key)
    if item['data'].get('parentItem') != parent:
        raise IntakeBlocked(f'attachment reparented concurrently: {key}')
    path = _path(zotero_data_dir() / 'storage', item)
    if sha256(path) != digest:
        raise IntakeBlocked(f'attachment content changed: {key}')
    return (item, path)

def _rename(client, key: str, filename: str, parent: str, digest: str, journal, undo: list, expected: dict):
    item, _ = _checked_attachment(client, key, parent, digest)
    if item['version'] != expected[key]['version'] or item['data'] != expected[key]['data']:
        raise IntakeBlocked(f'attachment changed before rename: {key}')
    if item['data']['filename'] == filename:
        return
    _name(filename)
    journal.write('rename_intent', key=key, before=item, filename=filename)
    client.rename_attachment(key, filename)
    after, _ = _checked_attachment(client, key, parent, digest)
    expected[key] = after
    undo.append((item, after))
    if after['data']['filename'] != filename:
        raise IntakeBlocked(f'rename readback failed: {key}')
    journal.write('rename_done', item=after)

def _import(client, plan: dict, source: Path, filename: str, title: str, journal) -> dict:
    parent = _get(client, plan['key'])
    children = _children(client, parent)
    wanted_role = 'annotated' if filename == f'{plan['citekey']}_annotated.pdf' else 'primary'
    matches = []
    for at in children:
        if at['data'].get('itemType') != 'attachment':
            continue
        role, _ = _infer(at, parent, 2, plan['roles'])
        if role == wanted_role:
            matches.append(at)
    if len(matches) > 1:
        raise IntakeBlocked(f'duplicate {wanted_role} attachments before import')
    if matches:
        if matches[0]['data'].get('filename') != filename:
            raise IntakeBlocked(f'concurrent {wanted_role} attachment needs re-plan')
        existing_path = _path(zotero_data_dir() / 'storage', matches[0])
        if wanted_role == 'primary' and sha256(existing_path) != sha256(source):
            raise IntakeBlocked('concurrent primary has different content; re-plan')
        journal.write('import_reused', item=matches[0])
        return matches[0]
    digest = sha256(source)
    token = hashlib.sha256(f'library-intake-v1:{plan['key']}:{wanted_role}:{digest}'.encode()).hexdigest()
    journal.write('import_intent', parent=plan['key'], source=str(source), filename=filename, source_sha256=digest, idempotency_key=token)
    client.import_attachment(plan['key'], str(source), filename, title, token)
    children = _children(client, _get(client, plan['key']))
    matches = [c for c in children if c['data'].get('itemType') == 'attachment' and c['data'].get('filename') == filename]
    if len(matches) != 1:
        raise IntakeBlocked('import outcome missing/duplicated; no automatic retry')
    result = matches[0]
    _checked_attachment(client, result['key'], plan['key'], digest)
    journal.write('import_done', item=result)
    return result

def _restore_metadata(client, before: dict, metadata: dict, journal, owned: dict | None) -> str:
    current = _get(client, before['key'])
    previous, changes = (metadata['previous'], metadata['changes'])
    if all((current['data'].get(k, '') == v for k, v in previous.items())):
        return 'unchanged'
    if owned is None:
        return 'manual_review: write outcome/version unconfirmed'
    if current['version'] != owned['version'] or current['data'] != owned['data']:
        return 'manual_review: concurrent parent edit; rollback refused'
    if any((current['data'].get(k, '') != v for k, v in changes.items() if k != 'citationKey')):
        return 'manual_review: changed metadata; rollback refused'
    journal.write('metadata_restore_intent', key=before['key'], changes=previous, version=current['version'])
    client.patch_item(before['key'], previous, current['version'])
    restored = _get(client, before['key'])
    if any((restored['data'].get(k, '') != v for k, v in previous.items())):
        return 'manual_review: restore readback differs'
    journal.write('metadata_restored', item=restored)
    return 'restored'

def apply_item(client: Client, plan: dict, *, swap: bool=False, allow_redo: bool=False) -> dict:
    result = {'key': plan['key'], 'citekey': plan.get('citekey', ''), 'status': 'blocked', 'primary_pdf': '', 'primary_key': '', 'source_sha256': '', 'redo_required': plan.get('redo_required', False), 'require_translation': plan.get('require_translation', True), 'blocked': list(plan.get('blocked', []))}
    if result['blocked']:
        return result
    if bool(plan.get('swap')) != swap:
        result['blocked'].append('swap authorization differs from plan; re-plan')
        return result
    if allow_redo and plan.get('refresh_source') is not True:
        result['blocked'].append('refresh authorization missing')
        return result
    try:
        fresh = _make_plan(client, plan['key'], swap, hints=plan.get('role_hints'), cache=_scan_cache(plan), allow_redo=allow_redo)
        _assert_snapshot(plan, fresh)
        with _Journal(plan['key']) as journal:
            result['journal'] = str(journal.path)
            journal.write('begin', plan=fresh)
            metadata, before = (fresh['metadata'], fresh['parent'])
            owned, undo, metadata_attempted = (None, [], False)
            try:
                if metadata['changes']:
                    journal.write('metadata_intent', key=plan['key'], changes=metadata['changes'], previous=metadata['previous'], version=before['version'])
                    metadata_attempted = True
                    response = client.patch_item(plan['key'], metadata['changes'], before['version'])
                    if isinstance(response, dict) and 'data' in response:
                        owned = _item(response, plan['key'])
                    else:
                        owned = _get(client, plan['key'])
                    journal.write('metadata_written', item=owned)
                    ready = _wait_key(client, plan['key'], metadata)
                    owned = ready
                    refreshed = _make_plan(client, plan['key'], swap, fresh['roles'], _scan_cache(fresh), allow_redo=allow_redo)
                    if refreshed['blocked']:
                        raise IntakeBlocked('; '.join(refreshed['blocked']))
                    if refreshed['metadata']['changes'] or refreshed['hashes'] != fresh['hashes']:
                        raise IntakeBlocked('source or metadata changed while waiting for BBT')
                    child_changes = _assert_bbt_children(fresh, refreshed)
                    if child_changes:
                        journal.write('bbt_auto_rename_verified', changes=child_changes, hashes=refreshed['hashes'])
                    if (LIB / 'papers' / refreshed['citekey']).exists() and refreshed['citekey'] != fresh['citekey']:
                        raise IntakeBlocked('new citation key collides with an existing Library folder')
                    fresh = refreshed
                    journal.write('replanned', plan=fresh)
                result['citekey'] = fresh['citekey']
                expected_attachments = {x['key']: x for x in fresh['children'] if x['data'].get('itemType') == 'attachment'}
                for action in fresh['actions']:
                    kind = action['action']
                    if kind == 'rename':
                        ak = action['attKey']
                        _rename(client, ak, action['newName'], plan['key'], fresh['hashes'][ak], journal, undo, expected_attachments)
                    elif kind in {'copy_as_annotated', 'swap_annotated_primary', 'attach_clean_primary'}:
                        ak = action.get('srcAttKey') or action['attKey']
                        current, source = _checked_attachment(client, ak, plan['key'], fresh['hashes'][ak])
                        if current['version'] != expected_attachments[ak]['version'] or current['data'] != expected_attachments[ak]['data']:
                            raise IntakeBlocked(f'attachment changed before copy: {ak}')
                        if kind != 'copy_as_annotated':
                            if not swap:
                                raise IntakeBlocked('swap authorization required')
                            clean = journal.path.parent / (journal.path.stem + '_clean.pdf')
                            journal.write('clean_intent', source=str(source), destination=str(clean))
                            ok, note = strip_annotations(source, clean)
                            if not ok:
                                raise IntakeBlocked(f'clean-copy verification failed: {note}')
                            checked = scan_pdf(str(clean), max_draw_pages=0)
                            if checked['err'] or checked['embed_markup'] or checked['flat_marks']:
                                raise IntakeBlocked('cleaned PDF still has marks or scan errors')
                            if kind == 'swap_annotated_primary':
                                _rename(client, ak, action['annotatedName'], plan['key'], fresh['hashes'][ak], journal, undo, expected_attachments)
                                fresh['roles'][ak] = {'role': 'annotated', 'suffix': '_annotated.pdf'}
                            source = clean
                        _import(client, fresh, source, action['newName'], action.get('title') or Path(action['newName']).stem, journal)
                    elif kind == 'move_annotations':
                        children = _children(client, _get(client, plan['key']))
                        targets = [x for x in children if x['data'].get('filename') == action['toName']]
                        if len(targets) != 1:
                            raise IntakeBlocked('annotation destination missing/duplicated')
                        for old in fresh['annotations'][action['fromAttKey']]:
                            current = _get(client, old['key'])
                            if current != old:
                                raise IntakeBlocked('annotation changed before relocation')
                            changes = {'parentItem': targets[0]['key']}
                            journal.write('annotation_intent', before=old, changes=changes)
                            client.patch_item(old['key'], changes, old['version'])
                            after = _get(client, old['key'])
                            expected = old['data'] | changes
                            if any((after['data'].get(k) != v for k, v in expected.items() if k not in {'version', 'dateModified'})):
                                raise IntakeBlocked('annotation relocation changed content/key')
                            journal.write('annotation_done', item=after)
                    elif kind in {'add_tag', 'remove_tag'}:
                        parent = _get(client, plan['key'])
                        tags = copy.deepcopy(parent['data'].get('tags', []))
                        if kind == 'add_tag' and (not any((t['tag'] == action['tag'] for t in tags))):
                            tags.append({'tag': action['tag']})
                        elif kind == 'remove_tag':
                            tags = [t for t in tags if t['tag'] != action['tag']]
                        if tags != parent['data'].get('tags', []):
                            journal.write('tag_intent', before=parent, tags=tags)
                            client.patch_item(plan['key'], {'tags': tags}, parent['version'])
                            owned = _get(client, plan['key'])
                            if owned['data'].get('tags') != tags:
                                raise IntakeBlocked('tag readback failed')
                    else:
                        raise IntakeBlocked(f'unsupported normalization action: {kind}')
                final = _make_plan(client, plan['key'], swap, cache=_scan_cache(fresh), allow_redo=allow_redo)
                if final['blocked'] or final['actions'] or final['metadata']['changes']:
                    raise IntakeBlocked('final verification incomplete: ' + '; '.join(final['blocked']))
                if any((final['hashes'].get(k) != digest for k, digest in fresh['hashes'].items())):
                    raise IntakeBlocked('original attachment content changed during apply')
                result.update(status='applied', citekey=final['citekey'], primary_pdf=final['primary_pdf'], primary_key=final['primary_key'], source_sha256=final['source_sha256'], require_translation=final['require_translation'], redo_required=result['redo_required'] or final['redo_required'], blocked=[])
                journal.write('complete', result=result)
            except Exception as exc:
                result.update(status='failed', error=f'{type(exc).__name__}: {exc}', manual_review=True)
                journal.write('failed', error=result['error'])
                if metadata_attempted:
                    try:
                        result['metadata_restore'] = _restore_metadata(client, before, metadata, journal, owned)
                    except Exception as restore_exc:
                        result['metadata_restore'] = f'manual_review: {restore_exc}'
                    if result['metadata_restore'] == 'restored':
                        renames = {}
                        for original, changed in undo:
                            first = renames.get(original['key'], (original, changed))[0]
                            renames[original['key']] = (first, changed)
                        for original, changed in reversed(list(renames.values())):
                            current = _get(client, changed['key'])
                            if current != changed:
                                journal.write('rename_restore_skipped', key=changed['key'], reason='concurrent change')
                                continue
                            journal.write('rename_restore_intent', before=current, filename=original['data']['filename'])
                            client.rename_attachment(original['key'], original['data']['filename'])
                journal.write('recovery', result=result)
    except Exception as exc:
        result['blocked'].append(f'{type(exc).__name__}: {exc}')
    return result
