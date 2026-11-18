# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid
import library_sync as s
LEGACY_RUN = None
LEGACY_PAPERS = {}
SAFE_PHASES = {None, 'preflight', 'snapshot', 'index_stage'}
SHA = re.compile('[0-9a-f]{64}\\Z')
RUN_ID = re.compile('[0-9]{8}_[0-9]{6}_[0-9a-f]{6}\\Z')

def read_json(path):

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique)

def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or (':' in relative) or any((ord(c) < 32 for c in relative)):
        raise ValueError('Invalid relative path')
    parts = relative.split('/')
    if any((not p or p in ('.', '..') or p.endswith(('.', ' ')) for p in parts)):
        raise ValueError('Non-canonical relative path')
    if PurePosixPath(relative).is_absolute():
        raise ValueError('Absolute paths are forbidden')
    root = Path(root)
    target = root.joinpath(*parts)
    cursor = root
    for part in parts:
        cursor /= part
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise ValueError('Symlink/junction paths are forbidden')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path escapes its root')
    return target

def expected_note_hash(paper, before, qa_frozen):
    ck = paper['citekey']
    allowed = {str(safe_path(s.LIBRARY, f'papers/{ck}/{ck}_{suffix}.md')) for suffix in ('EN', 'MD', 'ZH', 'Note')}
    note = safe_path(s.LIBRARY, f'papers/{ck}/{ck}_Note.md')
    original = before.get(str(note))
    if str(note) in before and (not isinstance(original, str) or not SHA.fullmatch(original)):
        raise RuntimeError('Invalid original Note hash')
    refresh = paper.get('source_refresh', {})
    if not isinstance(refresh, dict) or not isinstance(refresh.get('reanchored', {}), dict):
        raise RuntimeError('Invalid source refresh receipt')
    reanchored = refresh.get('reanchored', {})
    if any((not isinstance(name, str) or name not in allowed or (not isinstance(digest, str)) or (not SHA.fullmatch(digest)) for name, digest in reanchored.items())):
        raise RuntimeError('Invalid reanchored path/hash')
    if str(note) not in reanchored:
        return original
    expected = reanchored[str(note)]
    if original is None or paper.get('status') != 'applied' or paper.get('blocked') or paper.get('redo_required') or any((not isinstance(paper.get(field), str) or not pattern.fullmatch(paper[field]) for field, pattern in (('key', re.compile('[A-Z0-9]{8}')), ('primary_key', re.compile('[A-Z0-9]{8}')), ('source_sha256', SHA)))):
        raise RuntimeError('Reanchored Note lacks a valid source/baseline receipt')
    qa = paper.get('qa', {})
    if not isinstance(qa, dict):
        raise RuntimeError('Invalid reanchored Note QA receipt')
    if qa.get('ok') is True:
        metrics = qa.get('metrics', {})
        hashes = metrics.get('input_sha256', {}) if isinstance(metrics, dict) else {}
        if qa.get('errors') or not isinstance(hashes, dict) or (not isinstance(qa_frozen, dict)) or (hashes.get(note.name) != expected) or (qa_frozen.get(str(note)) != expected):
            raise RuntimeError('Reanchored Note differs from its QA/frozen receipt')
        return expected
    backup_name = refresh.get('backup')
    if not isinstance(backup_name, str):
        raise RuntimeError('Missing reanchored Note source archive')
    candidate = Path(backup_name)
    run_id = candidate.parent.name
    if not RUN_ID.fullmatch(run_id):
        raise RuntimeError('Invalid reanchored Note archive run')
    backup = safe_path(s.LIBRARY.resolve(), f".trash/source_refresh/{run_id}/{paper['key']}")
    if str(backup) != backup_name:
        raise RuntimeError('Invalid reanchored Note archive path')
    manifest_path = safe_path(backup, 'manifest.json')
    old_note = safe_path(backup, note.name)
    guards = {str(manifest_path): s.sha256(manifest_path), str(old_note): original}
    manifest = read_json(manifest_path)
    previous = manifest.get('previous', {}) if isinstance(manifest, dict) else {}
    old_key = previous.get('item_key') if isinstance(previous, dict) else None
    if not isinstance(manifest, dict) or manifest.get('reanchor') is not True or manifest.get('folder') != str(note.parent) or (not isinstance(manifest.get('files'), dict)) or (manifest['files'].get(str(note)) != original) or (not isinstance(old_key, str)) or (not re.fullmatch('[A-Z0-9]{8}', old_key)) or (old_key == paper['key']) or (manifest.get('selected_primary') != paper['primary_key']):
        raise RuntimeError('Invalid reanchored Note source manifest')
    data = old_note.read_bytes()
    from library_refresh import _reanchor
    if hashlib.sha256(data).hexdigest() != original or hashlib.sha256(_reanchor(data, old_key, paper['key'])).hexdigest() != expected:
        raise RuntimeError('Reanchored Note is not the recorded anchor-only change')
    s.assert_hashes(guards, 'Reanchored Note source receipts')
    return expected

def source_record(run_id):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('Invalid original RUN_ID')
    path = safe_path(s.RUNS, run_id + '/state.json')
    digest = s.sha256(path)
    state = read_json(path)
    if state.get('id') != run_id or state.get('status') != 'blocked' or state.get('resume_of'):
        raise RuntimeError('Only an original blocked run can be finalized')
    s.translation_enabled(state)
    if state.get('library_commit') or state.get('tools_commit') or state.get('index_backup') or (state.get('finalize_phase') not in SAFE_PHASES):
        raise RuntimeError('Original run may have published/committed; manual reconciliation required')
    papers = state.get('papers', [])
    if not papers or len({p['key'] for p in papers}) != len(papers):
        raise RuntimeError('Missing/duplicate original paper receipts')
    if len({p['citekey'] for p in papers}) != len(papers):
        raise RuntimeError('Duplicate original citekeys')
    if set(state.get('requested', [])) != {p['key'] for p in papers}:
        raise RuntimeError('Original requested batch is not fully successful')
    legacy = 'baseline' not in state
    guards = {str(path): digest}
    if legacy and (run_id != LEGACY_RUN or {p['key']: (p['citekey'], p.get('primary_key')) for p in papers} != LEGACY_PAPERS):
        raise RuntimeError('Missing ownership baseline; legacy recovery is not supported in this distribution')
    for position, p in enumerate(papers):
        if not re.fullmatch('[A-Z0-9]{8}', str(p.get('key', ''))):
            raise RuntimeError('Invalid original paper key')
        baseline_path = safe_path(s.RUNS, run_id + f"/remedy_baseline_{p['key']}.json")
        if not legacy and baseline_path.is_file():
            from library_remedy import _effective_paper, PUBLISH_SCOPE
            baseline_digest = s.sha256(baseline_path)
            saved = read_json(baseline_path)
            if not isinstance(saved.get('attempt'), str) or not RUN_ID.fullmatch(saved['attempt']):
                raise RuntimeError('单篇处置凭据编号无效')
            completion_path = safe_path(s.RUNS, saved['attempt'] + '/remedy_state.json')
            completion_digest = s.sha256(completion_path)
            record = read_json(completion_path)
            if record.get('status') != 'completed' or record.get('result', {}).get('ok') is not True or saved.get('attempt') != record.get('run_id') or (record.get('key') != p['key']) or (record.get('source_run') != run_id) or (record.get('action') not in PUBLISH_SCOPE):
                raise RuntimeError('单篇处置尚未完成或凭据不一致；未进入收尾')
            candidate = _effective_paper(s.RUNS, run_id, p)
            if (candidate.get('qa') or {}).get('ok') is not True:
                raise RuntimeError('单篇处置产物在验收后变化；请复查')
            folder = safe_path(s.LIBRARY, 'papers/' + p['citekey'])
            published = set(record['result'].get('published', []))
            scope = PUBLISH_SCOPE[record['action']]
            for name in published:
                member = Path(name)
                if not member.is_absolute() or not member.is_relative_to(folder):
                    raise RuntimeError('处置发布路径超出文献目录或不是规范路径')
                safe_path(s.LIBRARY, member.relative_to(s.LIBRARY).as_posix())
                relative = member.relative_to(folder)
                if not (scope == ('*',) and (relative.parts[0] == 'assets' or member.name in {p['citekey'] + '_' + kind + '.md' for kind in ('EN', 'MD', 'ZH')}) or (len(relative.parts) == 1 and member.name in {p['citekey'] + '_' + kind + '.md' for kind in scope})):
                    raise RuntimeError('处置发布路径超出动作范围')
            fresh = s.validate_completed_paper(candidate)
            if candidate['qa']['ok'] is not True:
                raise RuntimeError('单篇处置收尾前复验未通过')
            historical = state.get('qa_frozen', {})
            normalized_published = {str(Path(name).resolve()) for name in published}
            if any((str(Path(name).resolve()) not in normalized_published and historical.get(name) != value for name, value in fresh.items())):
                raise RuntimeError('处置范围外的原文、笔记、图片或 PDF 变化；未进入收尾')
            state.setdefault('qa_frozen', {}).update(fresh)
            p = candidate
            papers[position] = p
            guards.update({str(baseline_path): baseline_digest, str(completion_path): completion_digest})
        if not re.fullmatch('[^<>:"/\\\\|?*\\x00-\\x1f]+', p['citekey']):
            raise RuntimeError('Invalid citekey path component')
        safe_path(s.LIBRARY, 'papers/' + p['citekey'])
        if not re.fullmatch('[A-Z0-9]{8}', p['key']) or not re.fullmatch('[A-Z0-9]{8}', p.get('primary_key', '')) or p.get('status') != 'applied' or (p.get('exit_code') != 0) or (type(p.get('exit_code')) is not int) or p.get('blocked') or p.get('redo_required') or (p.get('qa', {}).get('ok') is not True) or p['qa'].get('errors') or (not SHA.fullmatch(p.get('source_sha256', ''))):
            raise RuntimeError('Original paper is not a successful QA/exit-zero receipt')
        hashes = p['qa'].get('metrics', {}).get('input_sha256', {})
        required = {f"{p['citekey']}_Note.md"}
        kind = p['qa'].get('metrics', {}).get('fulltext_kind')
        if kind not in ('EN', 'MD'):
            raise RuntimeError('Missing original fulltext kind')
        required.add(f"{p['citekey']}_{kind}.md")
        zh_name = f"{p['citekey']}_ZH.md"
        if s.paper_translation_required(p):
            required.add(zh_name)
        if not required <= set(hashes) <= required | {zh_name} or not all((isinstance(h, str) and SHA.fullmatch(h) for h in hashes.values())):
            raise RuntimeError('Invalid original QA hashes')
    s.assert_hashes(guards, 'Original and remedy receipts')
    return (state, guards, legacy)

def assert_no_previous_attempt(run_id, current_id=None):
    for path in s.RUNS.glob('*/state.json'):
        previous = read_json(path)
        if previous.get('resume_of') != run_id or previous.get('id') == current_id:
            continue
        if previous.get('status') != 'blocked' or previous.get('finalize_phase') not in SAFE_PHASES or previous.get('library_commit') or previous.get('index_backup'):
            raise RuntimeError('Previous finalize attempt completed or has uncertain/partial publication; refusing replay')

def reviewed_hashes(path, original, original_hashes):
    expected = dict(original_hashes)
    if path is None:
        return (expected, {}, None)
    path = Path(path).resolve(strict=True)
    guard = {str(path): s.sha256(path)}
    receipt = read_json(path)
    if set(receipt) != {'schema', 'run_id', 'files'} or type(receipt['schema']) is not int or receipt['schema'] != 1 or (receipt['run_id'] != original['id']) or (not isinstance(receipt['files'], list)) or (not receipt['files']):
        raise ValueError('Invalid reviewed-changes schema/run_id/files')
    allowed = {f"papers/{p['citekey']}/{p['citekey']}_{suffix}.md" for p in original['papers'] for suffix in ('EN', 'ZH')}
    seen = set()
    for entry in receipt['files']:
        if not isinstance(entry, dict) or set(entry) != {'path', 'before_sha256', 'after_sha256'}:
            raise ValueError('Invalid reviewed-changes file fields')
        target = safe_path(s.LIBRARY, entry['path'])
        relative = entry['path']
        if relative not in allowed or relative in seen or str(target) not in original_hashes:
            raise ValueError('Only unique original batch EN/ZH paths can be reviewed')
        seen.add(relative)
        before, after = (entry['before_sha256'], entry['after_sha256'])
        if not isinstance(before, str) or not isinstance(after, str) or (not SHA.fullmatch(before)) or (not SHA.fullmatch(after)) or (before == after) or (before != original_hashes[str(target)]) or (not target.is_file()) or (s.sha256(target) != after):
            raise RuntimeError('Reviewed change before/after SHA256 mismatch')
        expected[str(target)] = after
    s.assert_hashes(guard, 'Reviewed manifest')
    return (expected, guard, receipt)

def verify_identity(snapshot, papers):
    items = snapshot['items']
    by_key = {p['key']: p for p in items}
    if len(by_key) != len(items):
        raise RuntimeError('Duplicate live Zotero keys')
    for p in papers:
        parent = by_key.get(p['key'], {}).get('data', {})
        primary = by_key.get(p['primary_key'], {}).get('data', {})
        candidates = [x['key'] for x in items if not x['data'].get('deleted') and x['data'].get('parentItem') == p['key'] and (x['data'].get('itemType') == 'attachment') and (x['data'].get('contentType') == 'application/pdf') and (x['data'].get('filename') == p['citekey'] + '.pdf')]
        if parent.get('deleted') or parent.get('citationKey') != p['citekey'] or parent.get('itemType') in (None, 'attachment', 'annotation', 'note') or primary.get('deleted') or (primary.get('itemType') != 'attachment') or (primary.get('parentItem') != p['key']) or (primary.get('contentType') != 'application/pdf') or (primary.get('filename') != p['citekey'] + '.pdf') or (candidates != [p['primary_key']]):
            raise RuntimeError('Live Zotero citekey/primary PDF identity changed or missing')
    return by_key

def prepare(client, run_id, reviewed_changes=None, current_id=None):
    s.assert_idle()
    assert_no_previous_attempt(run_id, current_id)
    original, guards, legacy = source_record(run_id)
    s.assert_unstaged()
    dirty, tools_dirty = (s.dirty_paths(s.LIBRARY), s.dirty_paths(s.state_repo()))
    if any((p.startswith('indexes/') for p in dirty)):
        raise RuntimeError('indexes has user dirty files; refusing publication')
    before_index = s.index_fingerprint()
    snapshot_path = safe_path(s.RUNS, run_id + '/snapshot.json')
    guards[str(snapshot_path)] = s.sha256(snapshot_path)
    source_map = verify_identity(read_json(snapshot_path), original['papers'])
    original_hashes, owned = ({}, [])
    for p in original['papers']:
        folder = safe_path(s.LIBRARY, 'papers/' + p['citekey'])
        for suffix in ('EN', 'MD', 'ZH', 'Note'):
            doc = safe_path(s.LIBRARY, f"papers/{p['citekey']}/{p['citekey']}_{suffix}.md")
            owned.append(doc)
            original_hashes[str(doc)] = p['qa']['metrics']['input_sha256'].get(doc.name)
        if legacy:
            summaries = [json.loads(line.split('SUMMARY ', 1)[1].split(' pending_', 1)[0]) for line in p.get('qc_log', []) if line.startswith('SUMMARY ')]
            if len(summaries) != 1 or any((summaries[0].get(k) != 1 for k in ('md', 'qc', 'zh', 'note'))):
                raise RuntimeError('Legacy receipt does not prove new EN/QC/ZH/Note creation')
            note = folder / f"{p['citekey']}_Note.md"
            if not s.pristine_note(note, source_map[p['key']]):
                raise RuntimeError('Legacy Note is not pristine; ownership cannot be proved')
        family = client.parent_children(p['key'])
        verify_identity({'items': [family['parent'], *family['children']]}, [p])
        current_pdf = client.file_path(p['primary_key'])
        if Path(current_pdf).resolve() != Path(p['primary_pdf']).resolve():
            raise RuntimeError('Live primary PDF path changed')
        s.assert_hashes({str(Path(p['primary_pdf'])): p['source_sha256']}, 'Primary PDF')
    expected, receipt_guard, receipt = reviewed_hashes(reviewed_changes, original, original_hashes)
    guards.update(receipt_guard)
    s.assert_hashes(expected, 'Original QA/reviewed paper')
    existing = {p.relative_to(s.LIBRARY).as_posix() for p in owned if original_hashes[str(p)] is not None}
    untracked = set(filter(None, s.git(s.LIBRARY, 'ls-files', '--others', '--exclude-standard', '-z', '--', *sorted(existing)).split('\x00')))
    if legacy:
        if not existing <= untracked:
            raise RuntimeError('Legacy paper docs must all be untracked, including Note')
        before, baseline_dirty = ({}, dirty - existing)
    else:
        baseline = original['baseline']
        if baseline.get('schema') != 1 or set(baseline.get('owned', [])) != {str(p) for p in owned}:
            raise RuntimeError('Invalid ownership baseline')
        before = baseline['before']
        if not isinstance(before, dict) or not set(before) <= {str(p) for p in owned}:
            raise RuntimeError('Invalid baseline file paths')
        baseline_dirty = set(baseline['library_dirty'])
        if not set(before) <= set(original_hashes) or any((not SHA.fullmatch(h) for h in before.values())):
            raise RuntimeError('Invalid baseline hashes')
        newly_created = {p.relative_to(s.LIBRARY).as_posix() for p in owned if str(p) not in before and original_hashes[str(p)] is not None}
        if not newly_created <= untracked:
            raise RuntimeError('New batch files became tracked since original QA; review required')
        if before_index != baseline['before_index']:
            raise RuntimeError('Indexes changed since original baseline')
        historical_frozen = dict(original.get('qa_frozen', {}))
        if not historical_frozen or any((historical_frozen.get(k) != v for k, v in original_hashes.items())):
            raise RuntimeError('Missing/inconsistent original frozen receipt')
        historical_frozen.update(expected)
        s.assert_hashes(historical_frozen, 'Original frozen input')
    papers, frozen = (copy.deepcopy(original['papers']), {})
    for p in papers:
        expected_note = expected_note_hash(p, before, original.get('qa_frozen', {}))
        p['original_qa'] = copy.deepcopy(p['qa'])
        frozen.update(s.validate_completed_paper(p))
        if p['qa']['ok'] is not True:
            raise RuntimeError('Resume deterministic QA failed')
        note = str(s.LIBRARY / 'papers' / p['citekey'] / f"{p['citekey']}_Note.md")
        if note in before and expected_note != frozen[note]:
            raise RuntimeError('Existing Note changed; refusing recovery')
    if not legacy and set(frozen) != set(original['qa_frozen']):
        raise RuntimeError('Files added/removed since original QA')
    s.assert_hashes(expected, 'Original QA/reviewed paper')
    s.assert_hashes(guards, 'Recovery receipts')
    s.assert_indexes(before_index)
    s.assert_idle()
    return dict(original=original, papers=papers, frozen=frozen, before=before, owned=owned, source_map=source_map, dirty=baseline_dirty, tools_dirty=tools_dirty, before_index=before_index, guards=guards, receipt=receipt)

def assert_workflow_available():
    path = s.RUNS / 'workflow.lock'
    if not path.exists():
        return
    if os.name != 'nt':
        from library_platform import IS_MAC, posix_guard
        if not IS_MAC:
            raise RuntimeError('Unsupported operating system')
        try:
            with posix_guard(path):
                pass
        except OSError as error:
            raise RuntimeError('Another workflow holds the lock; recovery preview stopped') from error
        return
    import msvcrt
    with path.open('rb') as stream:
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError('Another workflow holds the lock; recovery preview stopped') from error
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

def resume_finalize(client, run_id, *, apply=False, reviewed_changes=None, attempt_id=None):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('Invalid original RUN_ID')
    original_path = safe_path(s.RUNS, run_id + '/state.json')
    candidate = read_json(original_path)
    if candidate.get('control_schema') == 1 and candidate.get('published_indexes'):
        if reviewed_changes:
            raise RuntimeError('索引发布后有人工修改需另行审查；不自动替换已发布哈希')
        from library_control import resume_published
        return resume_published(client, run_id, apply=apply, attempt=attempt_id)
    if not apply:
        assert_workflow_available()
        prepared = prepare(client, run_id, reviewed_changes)
        verify_identity(s.live_snapshot(client.stable_snapshot()), prepared['papers'])
        for p in prepared['papers']:
            if any((prepared['frozen'].get(k) != v for k, v in s.paper_fingerprint(p).items())):
                raise RuntimeError('Paper changed during recovery preview')
        s.assert_hashes(prepared['frozen'], 'Preview QA inputs')
        s.assert_hashes(prepared['guards'], 'Recovery receipts')
        s.assert_indexes(prepared['before_index'])
        s.assert_unstaged()
        s.assert_idle()
        assert_workflow_available()
        print(f"仅预览恢复收尾 {run_id}：{len(prepared['papers'])} 篇已通过复验；将只重建索引、选择性提交并保存报告（不发送桌面通知）。未写入状态、报告或台账。")
        return 0
    attempt = attempt_id or dt.datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6]
    if not RUN_ID.fullmatch(attempt):
        raise ValueError('Invalid recovery attempt id')
    if (s.RUNS / attempt / 'state.json').exists():
        raise RuntimeError('恢复 attempt 已执行，禁止覆盖历史')
    with s.controller_lock(attempt):
        run = safe_path(s.RUNS, attempt)
        run.mkdir(parents=True, exist_ok=True)
        report = s.REPORTS / f'library_sync_{attempt}.md'
        state = {'id': attempt, 'resume_of': run_id, 'started': dt.datetime.now().isoformat(timespec='microseconds'), 'status': 'running', 'finalize_phase': 'preflight', 'papers': [], 'requested': [], 'warnings': [], 'deferred': [], 'chunk_parallel': 1, 'report': str(report), 'controller': s.progress.process_identity(os.getpid()), 'monitor_done': False}
        tools_dirty, ledger = (set(), None)

        def checkpoint():
            s.progress_checkpoint(state, run)
        try:
            checkpoint()
            prepared = prepare(client, run_id, reviewed_changes, attempt)
            tools_dirty = prepared['tools_dirty']
            if (s.RUNS / 'discovery.json').exists():
                from library_discovery import load
                ledger = load(s.RUNS)
            state.update(papers=prepared['papers'], requested=[p['key'] for p in prepared['papers']], reviewed_changes=prepared['receipt'], original_receipts=prepared['guards'], qa_frozen=prepared['frozen'])
            checkpoint()
            s.finalize(client, state, run, **{k: prepared[k] for k in ('frozen', 'before_index', 'before', 'owned', 'source_map', 'dirty')}, commit=True, env=os.environ.copy(), checkpoint=checkpoint, verify_snapshot=lambda snap: verify_identity(snap, prepared['papers']), extra_guard=lambda: s.assert_hashes(prepared['guards'], 'Recovery receipts'))
        except Exception as error:
            state.update(status='blocked', error=f'{type(error).__name__}: {error}')
        return s.finish(state, run, report, commit=True, tools_dirty=tools_dirty, discovery_ledger=ledger)
