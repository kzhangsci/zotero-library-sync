# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import library_sync as s
from library_issues import CATEGORIES, classify_log, classify_qa, classify_reasons, load as load_issues, record_disposition, save as save_issues, undo_disposition
KEY_RX = re.compile('[A-Z0-9]{8}\\Z')
LOG_LINES = 60
LOG_CHARS = 8000
PUBLISH_SCOPE = {'repair_zh': ('ZH',), 'retranslate_zh': ('ZH',), 'retry_qc': ('EN', 'MD'), 'reparse_source': ('*',), 'migrate_metadata': ('ZH',)}
WORKER_ACTIONS = {'repair_zh': ('zh',), 'retranslate_zh': ('zh',), 'retry_qc': ('qc',), 'reparse_source': ('folders', 'md', 'qc')}
ACTION_RX = re.compile('[a-z_]{1,32}\\Z')

def _read(path):
    from library_resume import read_json
    return read_json(path)

def _clean(text, limit=300):
    from library_progress import _clean as progress_clean
    return progress_clean(text, limit)

def _paper_folder(citekey):
    from library_resume import safe_path
    if not isinstance(citekey, str) or not re.fullmatch('[^<>:"/\\\\|?*\\x00-\\x1f]+', citekey) or citekey in ('.', '..') or citekey.endswith(('.', ' ')):
        return None
    return safe_path(s.LIBRARY, 'papers/' + citekey)

def _find_paper(runs, run_id, key):
    from library_progress import _run_id, _read_json
    _run_id(run_id)
    if not KEY_RX.fullmatch(key):
        raise ValueError('invalid item key')
    seen = set()
    current = run_id
    while current and current not in seen:
        seen.add(current)
        state = _read_json(Path(runs) / current / 'state.json')
        if state is not None:
            for paper in state.get('papers', []):
                if isinstance(paper, dict) and paper.get('key') == key:
                    return (state, _effective_paper(runs, current, paper), current)
            current = state.get('worker_resume_of') or state.get('recovered_from') or state.get('resume_of')
        else:
            current = None
    state = _read_json(Path(runs) / run_id / 'state.json') or {}
    for paper in state.get('planned_papers', []):
        if isinstance(paper, dict) and paper.get('key') == key:
            return (state, dict(paper), run_id)
    raise RuntimeError('该任务不包含此文献；请核对任务与条目')

def _effective_paper(runs, run_id, paper, library=None):
    path = Path(runs) / run_id / f"remedy_baseline_{paper['key']}.json"
    saved = _read(path) if path.is_file() else {}
    baseline = saved.get('paper', {})
    if baseline and all((baseline.get(k) == paper.get(k) for k in ('key', 'citekey', 'primary_pdf', 'source_sha256'))):
        folder = Path(library) / 'papers' / paper['citekey'] if library else _paper_folder(paper.get('citekey', ''))
        primary = Path(baseline['primary_pdf'])
        if not primary.is_file() or s.sha256(primary) != baseline['source_sha256'] or any((not (folder / name).is_file() or s.sha256(folder / name) != digest for name, digest in _known_hashes(baseline).items())):
            return {**paper, **baseline, 'exit_code': None, 'progress_phase': 'failed', 'qa': {'ok': False, 'errors': [{'code': 'modified_after_remedy', 'message': '产物在最近一次验收后发生变化，请复查'}], 'metrics': baseline['qa'].get('metrics', {})}}
        return {**paper, **baseline, 'qc_log': [], 'blocked': []}
    return paper

def _log_tail(paper):
    path = paper.get('log')
    if not isinstance(path, str):
        return ''
    resolved = Path(path)
    if resolved.resolve().is_relative_to(s.RUNS.resolve()) and resolved.is_file():
        try:
            lines = resolved.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return ''
        return _clean('\n'.join(lines[-LOG_LINES:]), LOG_CHARS)
    return ''

def _artifacts(paper):
    citekey = paper.get('citekey')
    if not isinstance(citekey, str) or not citekey or any((c in citekey for c in '\\"/:*?<>|')) or ('..' in citekey):
        return {'files': [], 'qc_marked': False}
    folder = s.LIBRARY / 'papers' / citekey
    try:
        files = [suffix for suffix in ('EN', 'MD', 'ZH', 'Note') if (folder / f'{citekey}_{suffix}.md').is_file()]
        qc = False
        for suffix in ('EN', 'MD'):
            path = folder / f'{citekey}_{suffix}.md'
            if path.is_file():
                try:
                    qc = not s._fulltext_lacks_qc(path)
                except Exception:
                    qc = False
                if qc:
                    break
        return {'files': files, 'qc_marked': qc}
    except OSError:
        return {'files': [], 'qc_marked': False}

def _needs_action_reasons(state, key):
    for row in state.get('needs_action', []) or []:
        if isinstance(row, dict) and row.get('key') == key:
            return [str(x) for x in row.get('reasons', [])]
    return []

def _fulltext_present(files):
    return 'EN' in files or 'MD' in files

def _translation_required(paper):
    try:
        return bool(paper.get('require_translation', True)) and bool(paper.get('translate', True))
    except Exception:
        return True

def _worker_remedies(paper, categories, artifacts):
    names = {row['category'] for row in categories}
    files = artifacts.get('files', [])
    has_full = _fulltext_present(files)
    remedies = []
    remedies.append({'id': 'repair_zh', 'kind': 'worker', 'roles': ('qc', 'zh'), 'label': '定向修复译文（本地确定性修订优先，必要时仅重译问题单元；隔离候选、验收通过才写入）', 'available': bool(has_full and 'ZH' in files and ('translation' in names)), 'reason': '' if has_full and 'ZH' in files else '需要已有原文与译文'})
    remedies.append({'id': 'retranslate_zh', 'kind': 'worker', 'roles': ('zh',), 'label': '仅重新翻译中文（从核验过的当前 EN 生成全新 ZH 候选；先完整验收，再备份+原子替换；不重解析 PDF，不动 Note 与你的其它修改）', 'available': bool(has_full and 'EN' in files and _translation_required(paper)), 'reason': '' if has_full and 'EN' in files else '需要英文全文（MD 中文原文无需重译）'})
    remedies.append({'id': 'retry_qc', 'kind': 'worker', 'roles': ('qc',), 'label': '重跑英文质检（QC）步骤（隔离候选；验收通过才写回原文标记）', 'available': bool(has_full and ('english_qc' in names or 'legacy_format' in names)), 'reason': '' if has_full else '缺少原文全文，先在 Zotero 补齐正本'})
    remedies.append({'id': 'reparse_source', 'kind': 'worker', 'roles': ('qc', 'zh'), 'explicit_only': True, 'label': '显式重新解析 PDF（MinerU 全新解析，费用高；仅在明确选择后执行）', 'available': bool(paper.get('primary_key') and has_full), 'reason': '' if paper.get('primary_key') else '缺少正本附件记录'})
    return remedies

def _local_remedies(paper, categories, artifacts):
    names = {row['category'] for row in categories}
    files = artifacts.get('files', [])
    has_full = _fulltext_present(files)
    translation_satisfied = 'MD' in files or 'ZH' in files or (not _translation_required(paper))
    complete = bool(has_full and 'Note' in files and translation_satisfied)
    return [{'id': 'adopt_existing', 'kind': 'local', 'label': '采纳现有产物（仅核验身份与完整质检后记账；不调用模型）', 'available': bool(('valid_existing' in names or not names) and complete), 'reason': '' if complete else '产物不齐（原文/笔记/按需译文），不能按完成采纳'}, {'id': 'migrate_metadata', 'kind': 'local', 'label': '旧版元数据/格式迁移（确定性修复派生文件元数据；预览差异后写入）', 'available': bool('legacy_format' in names), 'reason': '' if 'legacy_format' in names else '未检测到可确定性迁移的元数据/格式问题'}]

def _instruction_remedies(categories, blocked_reasons):
    names = {row['category'] for row in categories}
    remedies = []
    if 'missing_pdf' in names:
        remedies.append({'id': 'fix_missing_pdf', 'kind': 'instruction', 'label': '缺少正本 PDF：请先在 Zotero 中为该条目补齐/修复 PDF 附件（正本须为无批注全文），保存待 Zotero 完成导入后，点击“复查此条目”。此问题不会用模型重试解决。', 'available': True, 'reason': ''})
    if 'identity' in names:
        remedies.append({'id': 'resolve_identity', 'kind': 'instruction', 'label': '身份/来源冲突：请在 Zotero 选中该条目及其唯一正本 PDF，用面板“重新检测/更换全文 PDF”流程显式选择并预演（按附件身份核验，不按标题猜测），确认后再执行。', 'available': True, 'reason': ''})
    if 'api' in names:
        remedies.append({'id': 'api_settings', 'kind': 'instruction', 'label': 'API/鉴权/超时/限流：请先到“设置”页核对并保存可用的模型配置与密钥，再对失败步骤选择“重跑英文质检”“定向修复译文”或“仅重新翻译中文”。', 'available': True, 'reason': ''})
    if any(('用户' in str(x) for x in blocked_reasons)) or 'user_modified' in names:
        remedies.append({'id': 'user_modified', 'kind': 'instruction', 'label': '产物包含用户修改：默认保留你的版本。可先对比差异；如需生成候选，请使用“定向修复译文”或“仅重新翻译中文”（隔离候选 + 先备份当前字节，验收通过才替换）。', 'available': True, 'reason': ''})
    if 'legacy_format' in names:
        remedies.append({'id': 'provenance_manual', 'kind': 'instruction', 'label': '旧全文来源不明时须人工核实：元数据迁移只对齐派生文件的可验证字段，不凭当前 PDF 文件名推断旧全文的来源正确性。', 'available': True, 'reason': ''})
    return remedies

def _ledger_remedies(ledger_row):
    dispositions = ledger_row.get('dispositions', []) if isinstance(ledger_row, dict) else []
    return [{'id': 'recheck', 'kind': 'ledger', 'label': '复查此条目（只读重新检测准备与产物；不调用模型，不写 Zotero）', 'available': True, 'reason': ''}, {'id': 'defer', 'kind': 'ledger', 'label': '暂缓本篇（记录原因，之后可撤销）', 'available': True, 'reason': ''}, {'id': 'exclude', 'kind': 'ledger', 'label': '排除本篇（自动发现不再选中；保留到你自己撤销）', 'available': True, 'reason': ''}, {'id': 'undo', 'kind': 'ledger', 'label': '撤销上一次处置', 'available': bool(dispositions), 'reason': '' if dispositions else '尚无可撤销的处置记录'}]

def diagnose(runs, run_id, key):
    state, paper, found_run = _find_paper(runs, run_id, key)
    artifacts = _artifacts(paper)
    qa = paper.get('qa') if isinstance(paper.get('qa'), dict) else None
    categories = classify_qa(qa) + classify_log(paper.get('qc_log', []))
    blocked = ([] if qa and qa.get('ok') else _needs_action_reasons(state, key)) + [str(x) for x in paper.get('blocked') or []]
    if blocked:
        categories = categories + classify_reasons(blocked, s.LIBRARY, paper.get('citekey', ''))
    if not categories and (paper.get('progress_phase') == 'paused' or paper.get('exit_code') == 75):
        categories = categories + [{'category': 'pending_paused', 'label': CATEGORIES['pending_paused'], 'evidence': [{'code': 'paused', 'message': '本篇在步骤边界暂停；继续任务即补缺失步骤'}]}]
    merged = []
    for row in categories:
        if row['category'] not in [m['category'] for m in merged]:
            merged.append(row)
        else:
            next((m for m in merged if m['category'] == row['category']))['evidence'].extend(row['evidence'])
    ledger_row = _ledger_row(runs, key)
    folder = _paper_folder(paper.get('citekey', ''))
    latest_path = Path(runs) / found_run / f'remedy_latest_{key}.json'
    latest = _read(latest_path) if latest_path.is_file() else {}
    return {'run_id': found_run, 'key': key, 'citekey': _clean(paper.get('citekey', ''), 120), 'title': _clean(paper.get('title', ''), 300), 'phase': paper.get('progress_phase') or ('validated' if qa and qa.get('ok') else 'unknown'), 'accepted': bool(qa and qa.get('ok') is True and (paper.get('exit_code') == 0)), 'exit_code': paper.get('exit_code'), 'classification': merged[:6], 'qa': {'ok': qa.get('ok'), 'errors': [{'code': _clean(e.get('code'), 60), 'message': _clean(e.get('message'), 300), **({'file': _clean(e.get('file'), 120)} if e.get('file') else {}), **({'unit': e.get('unit')} if e.get('unit') is not None else {})} for e in qa.get('errors', [])[:24]]} if qa else None, 'historical_failure': {'run_id': found_run, 'report': _clean(state.get('report'), 500)}, 'artifacts': artifacts, 'log_tail': _log_tail({'log': latest['result']['log']}) if latest.get('result', {}).get('log') else _log_tail(paper), 'latest_attempt': latest or {'run_id': found_run, 'exit_code': paper.get('exit_code'), 'progress_phase': paper.get('progress_phase'), 'finished': state.get('finished')}, 'paths': {'report': _clean(state.get('report'), 500), 'paper_folder': str(folder) if folder else '', 'paper_folder_relative': f"papers/{paper['citekey']}" if folder else '', 'zotero_uri': f'zotero://select/library/items/{key}'}, 'ledger': {'status': ledger_row.get('status'), 'reasons': [_clean(x, 200) for x in ledger_row.get('reasons', [])][:6], 'updated': ledger_row.get('updated'), 'dispositions': len(ledger_row.get('dispositions', []))} if isinstance(ledger_row, dict) else None, 'remedies': _worker_remedies(paper, merged, artifacts) + _local_remedies(paper, merged, artifacts) + _instruction_remedies(merged, blocked) + _ledger_remedies(ledger_row)}

def _ledger_row(runs, key):
    return (load_issues(runs).get('items', {}) or {}).get(key)

def _guard_idle():
    if s.active_controller():
        raise RuntimeError('已有同步任务运行；请等待或暂停当前任务后再执行处置动作')
    s.assert_idle()

def _identity_check(client, paper):
    from library_resume import verify_identity
    family = client.parent_children(paper['key'])
    family_items = [family['parent'], *family['children']]
    verify_identity({'items': family_items}, [paper])
    current = client.file_path(paper['primary_key'])
    if Path(current).resolve() != Path(paper['primary_pdf']).resolve():
        raise RuntimeError('Zotero 正本路径已变化；请先通过“更换全文 PDF”流程重新核验')
    s.assert_hashes({paper['primary_pdf']: paper['source_sha256']}, 'Remedy primary')
    return family_items

def _known_hashes(paper):
    known = {}
    qa = paper.get('qa') if isinstance(paper.get('qa'), dict) else {}
    metrics = qa.get('metrics', {}) if isinstance(qa.get('metrics', {}), dict) else {}
    for name, digest in (metrics.get('input_sha256') or {}).items():
        known[str(name)] = digest
    return known

def _folder_hashes(folder, citekey):
    if folder is None or not folder.is_dir():
        return {}
    return {p.relative_to(folder).as_posix(): s.sha256(p) for p in sorted(folder.rglob('*')) if p.is_file() and (not p.name.startswith('.sync_'))}

def _user_edit_conflict(paper, folder, suffix):
    if folder is None:
        return None
    path = folder / f"{paper['citekey']}_{suffix}.md"
    if not path.is_file():
        return None
    known = _known_hashes(paper)
    if path.name in known and known[path.name] != s.sha256(path):
        return f'{path.name} 的当前字节与任务记录不同（疑似你的修改，默认保留）；如确认用流水线候选替换，请勾选“覆盖我的修改”（原字节会先备份）'
    return None

def binding(client, runs, run_id, key, action):
    _, paper, found_run = _find_paper(runs, run_id, key)
    folder = _paper_folder(paper.get('citekey', ''))
    from model_api import load_config, validate_config
    saved = load_config() if action in WORKER_ACTIONS else None
    model = None
    if saved is not None:
        model = validate_config(saved)
    return {'key': key, 'run': found_run, 'action': action, 'citekey': paper.get('citekey', ''), 'primary': s.sha256(Path(paper['primary_pdf'])) if paper.get('primary_pdf') and Path(paper['primary_pdf']).is_file() else None, 'primary_path': paper.get('primary_pdf'), 'identity': paper.get('primary_key'), 'ledger': _ledger_row(runs, key), 'files': _folder_hashes(folder, paper['citekey']), 'model': model}

def _model_config_for(steps):
    from model_api import load_config, validate_config
    saved = load_config()
    if saved is None:
        raise RuntimeError('尚未保存模型配置；请先到设置页保存（处置动作不会退回默认模型）')
    roles = tuple((role for role, needed in (('qc', 'qc' in steps), ('zh', 'zh' in steps)) if needed)) or ('qc',)
    config = validate_config(saved, require_credentials=True, required_roles=roles)
    summary = '; '.join((f"{role}={config[role]['provider']}/{config[role]['model']}(effort={config[role]['reasoning_effort']})" for role in roles))
    return (config, summary)

def _stage_folder(runs, paper, folder, action):
    from library_control import new_attempt
    attempt = new_attempt()
    staged_library = s.CACHE / 'remedy_staging' / attempt / 'Library'
    staged = staged_library / 'papers' / paper['citekey']
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(folder, staged, ignore=shutil.ignore_patterns('.sync_*.lock'))
    if action == 'retranslate_zh':
        (staged / f"{paper['citekey']}_ZH.md").unlink(missing_ok=True)
    if action == 'reparse_source':
        for suffix in ('EN', 'MD'):
            (staged / f"{paper['citekey']}_{suffix}.md").unlink(missing_ok=True)
        shutil.rmtree(staged / 'assets', ignore_errors=True)
    return (attempt, staged_library, staged)

def _dispatch_staged(client, runs, paper, folder, action, family_items):
    steps = WORKER_ACTIONS[action]
    attempt, staged_library, staged = _stage_folder(runs, paper, folder, action)
    run = Path(runs) / attempt
    run.mkdir(parents=True, exist_ok=False)
    snapshot = s.live_snapshot({'time': dt.datetime.now().isoformat(timespec='seconds'), 'items': family_items, 'collections': []})
    s.save_json(run / 'snapshot.json', snapshot)
    s.save_json(run / 'remedy_state.json', {'kind': 'remedy', 'key': paper['key'], 'citekey': paper['citekey'], 'action': action, 'steps': list(steps), 'staged': str(staged), 'created': dt.datetime.now().isoformat(timespec='seconds')})
    config, _ = _model_config_for(steps)
    s.save_json(run / 'model_config.json', config)
    logfile = run / (paper['key'] + '.log')
    env = os.environ.copy()
    env.update(PYTHONUTF8='1', QC_PARALLEL='1', ZH_PARALLEL='1', GLM_MAX_CONCURRENCY='2')
    args = ['--only', paper['citekey'], '--snapshot', str(run / 'snapshot.json'), '--steps', ','.join(steps), '--apply', '--api-config', str(run / 'model_config.json'), '--worker-id', 'remedy_' + paper['key']]
    if action == 'reparse_source':
        args += ['--fresh-source', '--retry-failed']
    code = s.run_sync(args, logfile, env, library_root=staged_library)
    return (code, logfile, run, staged)

def _qa_staged(staged, paper):
    from library_qa import validate_paper
    return validate_paper(staged, paper['citekey'], paper['primary_pdf'], require_qc=True, require_translation=_translation_required(paper))

def _publish(staged, folder, citekey, action):
    scope = PUBLISH_SCOPE[action]
    if scope == ('*',):
        allowed = {f'{citekey}_{suffix}.md' for suffix in ('EN', 'MD', 'ZH')}
        targets = [p for p in staged.rglob('*') if p.is_file() and (p.relative_to(staged).as_posix() in allowed or p.relative_to(staged).parts[0] == 'assets')]
    else:
        targets = [p for p in (staged / f'{citekey}_{suffix}.md' for suffix in scope) if p.is_file()]
    changes = []
    for staged_file in targets:
        live = folder / staged_file.relative_to(staged)
        if live.is_file() and s.sha256(live) == s.sha256(staged_file):
            continue
        changes.append((staged_file, live))
    if not changes:
        return {'published': [], 'backup': None, 'backup_pairs': []}
    backup_root = s.CACHE / 'remedy_backups' / citekey
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='publish_', dir=backup_root))
    pairs = []
    originals = {str(live): s.sha256(live) if live.is_file() else None for _, live in changes}
    for _, live in changes:
        if live.is_file():
            saved = backup / live.relative_to(folder)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, saved)
            pairs.append((str(saved), str(live)))
    result = {'published': [str(live) for _, live in changes], 'backup': str(backup), 'backup_pairs': pairs, 'touched': [], 'created': [str(live) for _, live in changes if not live.exists()]}
    s.save_json(backup / 'transaction.json', {**result, 'status': 'prepared'})
    try:
        for staged_file, live in changes:
            current = s.sha256(live) if live.is_file() else None
            if current != originals[str(live)]:
                raise RuntimeError('发布期间文件被外部修改；请重新预览')
            live.parent.mkdir(parents=True, exist_ok=True)
            tmp = live.with_name(f'.{live.name}.{os.getpid()}.tmp')
            try:
                shutil.copyfile(staged_file, tmp)
                result['touched'].append(str(live))
                s.save_json(backup / 'transaction.json', {**result, 'status': 'publishing'})
                os.replace(tmp, live)
            finally:
                tmp.unlink(missing_ok=True)
        s.save_json(backup / 'transaction.json', {**result, 'status': 'published'})
    except Exception as error:
        rolled = _rollback(result)
        raise RuntimeError('发布失败；' + ('已回滚为原字节' if rolled else '回滚未完成，请从备份恢复：' + str(backup))) from error
    return result

def _rollback(publish_result):
    ok = True
    for saved, live in reversed(publish_result.get('backup_pairs', [])):
        if 'touched' in publish_result and live not in publish_result['touched']:
            continue
        try:
            temporary = Path(live).with_name('.' + Path(live).name + '.rollback.tmp')
            shutil.copyfile(saved, temporary)
            os.replace(temporary, live)
        except OSError:
            ok = False
        finally:
            if 'temporary' in locals():
                temporary.unlink(missing_ok=True)
    for live in publish_result.get('created', []):
        if 'touched' in publish_result and live not in publish_result['touched']:
            continue
        try:
            Path(live).unlink(missing_ok=True)
        except OSError:
            ok = False
    if publish_result.get('backup'):
        try:
            s.save_json(Path(publish_result['backup']) / 'transaction.json', {**publish_result, 'status': 'rolled_back' if ok else 'recovery_required'})
        except OSError:
            ok = False
    return ok

def _record_ledger(runs, key, *, action, status, reasons=(), release_hold=False, evidence=None, run_report=None, run_id=None):
    ledger = load_issues(runs)
    if key not in ledger.get('items', {}) and run_id:
        from library_issues import record_result
        state = _read(Path(runs) / run_id / 'state.json') if run_id else {}
        if isinstance(state, dict):
            record_result(ledger, state)
    record_disposition(ledger, key, action=action, resulting_status=status, reasons=reasons, release_hold=release_hold, evidence=evidence)
    if run_report:
        ledger['items'][key]['run_report'] = str(run_report)
    save_issues(runs, ledger)
    return ledger['items'][key]

def _mark_discovery_complete(runs, key, citekey, run_id):
    path = Path(runs) / 'discovery.json'
    if not path.is_file():
        return
    from library_discovery import timestamp
    ledger = _read(path)
    entry = ledger.setdefault('entries', {}).setdefault(key, {})
    state = _read(Path(runs) / run_id / 'state.json') or {}
    if entry.get('run_started') and timestamp(entry['run_started']) > timestamp(state.get('started') or entry['run_started']):
        return
    entry.update(status='completed', citekey=citekey, run_id=run_id, run_started=state.get('started', entry.get('run_started', '')), reason='')
    s.save_json(path, ledger)

def _fingerprints(paper):
    from library_issues import _fingerprints as fingerprints
    return fingerprints(paper)

def _assert_live_unchanged(paper, folder, before):
    if s.sha256(Path(paper['primary_pdf'])) != before['primary']:
        raise RuntimeError('正本在处置期间发生变化；已停止，请重新预览')
    if _folder_hashes(folder, paper['citekey']) != before['files']:
        raise RuntimeError('真实产物在处置期间被外部修改；请重新预览后再处置')

def _worker_remedy(client, runs, run_id, key, action, params, *, confirm):
    state, paper, found_run = _find_paper(runs, run_id, key)
    if not paper.get('primary_key') or not paper.get('primary_pdf'):
        raise RuntimeError('该条目在本任务中没有正本附件记录；请先重新预演或使用复查/暂缓')
    steps = WORKER_ACTIONS[action]
    folder = _paper_folder(paper['citekey'])
    if folder is None or not folder.is_dir():
        raise RuntimeError('citekey 无效或论文目录不存在')
    guard_suffixes = ['EN', 'MD'] if action != 'reparse_source' else []
    if action in ('repair_zh', 'retranslate_zh'):
        guard_suffixes.append('ZH')
    conflicts = [message for message in (_user_edit_conflict(paper, folder, suffix) for suffix in guard_suffixes) if message]
    before = {'primary': s.sha256(Path(paper['primary_pdf'])), 'files': _folder_hashes(folder, paper['citekey'])}
    if not confirm:
        _, summary = _model_config_for(steps)
        return {'preview': True, 'action': action, 'steps': list(steps), 'key': key, 'citekey': paper['citekey'], 'will_run_in': '隔离候选目录（真实文献目录不会被 worker 直接改动）', 'will_backup': sorted(before['files']), 'model_settings': f'当前已保存的模型配置（显式选择，不用冻结旧配置）：{summary}', 'user_edit_conflicts': conflicts, 'notes': ['候选先在隔离目录完整验收，通过才逐文件发布到真实目录', '发布前重新核验源文件/正本/Note 未变化；失败不发布任何字节', '正本与附件身份将先核验，不一致则拒绝执行']}
    if conflicts and params.get('overwrite') != 'user_edited_confirmed':
        raise RuntimeError('；'.join(conflicts))
    _guard_idle()
    family_items = _identity_check(client, paper)
    late = [message for message in (_user_edit_conflict(paper, folder, suffix) for suffix in guard_suffixes) if message]
    if late and params.get('overwrite') != 'user_edited_confirmed':
        raise RuntimeError('；'.join(late))
    code, logfile, run, staged = _dispatch_staged(client, runs, paper, folder, action, family_items)
    outcome = {'action': action, 'key': key, 'citekey': paper['citekey'], 'run': run.name, 'exit_code': code, 'log': str(logfile)}
    if code != 0:
        _assert_live_unchanged(paper, folder, before)
        _record_ledger(runs, key, action=f'remedy:{action}:failed', status='needs_repair', reasons=[f'处置 worker 退出码 {code}（仅作用于隔离候选；真实产物字节未变）'], run_report=state.get('report'), run_id=found_run)
        outcome.update(ok=False, published=[], message=f'处置步骤退出码 {code}；候选未完成执行。真实产物字节已核验未变，请查看日志。')
        return outcome
    qa = _qa_staged(staged, paper)
    if qa.get('ok') is not True:
        codes = sorted({str(e.get('code')) for e in qa.get('errors', []) if e.get('code')})
        _assert_live_unchanged(paper, folder, before)
        _record_ledger(runs, key, action=f'remedy:{action}:qa_failed', status='needs_repair', reasons=['候选完整验收未通过：' + ','.join(codes) + '；未发布任何字节'], run_report=state.get('report'), run_id=found_run)
        outcome.update(ok=False, published=[], message='隔离候选未通过完整验收（' + ','.join(codes) + '）；未发布任何字节，真实产物保持原样。请查看诊断与日志。')
        return outcome
    _assert_live_unchanged(paper, folder, before)
    published = _publish(staged, folder, paper['citekey'], action)
    working = dict(paper)
    working.pop('qa', None)
    try:
        s.validate_completed_paper(working)
        live_ok = working.get('qa', {}).get('ok') is True
    except Exception as error:
        live_ok = False
        outcome['post_error'] = f'{type(error).__name__}: {error}'
    if not live_ok:
        rolled = _rollback(published)
        codes = sorted({str(e.get('code')) for e in working.get('qa', {}).get('errors', []) if isinstance(e, dict) and e.get('code')})
        _record_ledger(runs, key, action=f'remedy:{action}:publish_rollback', status='needs_repair', reasons=['发布后复验未通过' + ('：' + ','.join(codes) if codes else '') + ('；已回滚为原字节' if rolled else '；回滚失败，请从备份恢复：' + str(published.get('backup')))], run_report=state.get('report'), run_id=found_run)
        outcome.update(ok=False, published=[], message='发布后复验未通过；' + ('已回滚为原字节。' if rolled else '回滚失败，原字节备份于 ' + str(published.get('backup'))))
        return outcome
    _record_ledger(runs, key, action=f'remedy:{action}', status='complete', reasons=[f'处置动作 {action} 候选通过完整验收并发布'], release_hold=True, evidence=_fingerprints(working), run_report=state.get('report'), run_id=found_run)
    _mark_discovery_complete(runs, key, paper['citekey'], found_run)
    outcome.update(ok=True, published=published['published'], backup=published['backup'], message='处置完成：隔离候选通过完整验收后发布；原字节备份于 ' + str(published['backup']))
    return outcome

def _adopt_remedy(client, runs, run_id, key, *, confirm):
    state, paper, found_run = _find_paper(runs, run_id, key)
    _identity_check(client, paper)
    working = copy.deepcopy(paper)
    from library_qa import validate_paper
    folder = _paper_folder(paper['citekey'])
    qa = validate_paper(folder, paper['citekey'], paper['primary_pdf'], require_qc=True, require_translation=_translation_required(working))
    result = {'action': 'adopt_existing', 'key': key, 'citekey': paper['citekey'], 'qa_ok': qa.get('ok') is True, 'errors': [{'code': _clean(e.get('code'), 60), 'message': _clean(e.get('message'), 300)} for e in qa.get('errors', [])[:16]], 'model_calls': 0}
    if not confirm:
        result['preview'] = True
        result['notes'] = ['身份核验：Zotero 条目/正本路径/正本字节与本任务记录一致', '不调用模型；仅当上方错误列表为空（完整验收通过）时确认才会生效']
        return result
    if qa.get('ok') is not True:
        raise RuntimeError('现有产物未通过完整验收；不能按完成采纳。请先处理上方列出的问题。')
    _record_ledger(runs, key, action='adopt_existing', status='complete', reasons=['现有产物通过身份核验与完整验收，按完成采纳（未调用模型）'], release_hold=True, evidence=_fingerprints(working), run_report=state.get('report'), run_id=found_run)
    _mark_discovery_complete(runs, key, paper['citekey'], found_run)
    result['ok'] = True
    result['message'] = '已采纳：完成账本已更新（无模型调用）。'
    return result

def _front_matter_fields(text):
    from sync import front_matter
    fm, _, body = front_matter(text)
    return (fm, body)

def _migrate_targets(paper, folder):
    full = next((folder / f"{paper['citekey']}_{kind}.md" for kind in ('EN', 'MD') if (folder / f"{paper['citekey']}_{kind}.md").is_file()), None)
    if full is None:
        raise RuntimeError('缺少原文全文；无可迁移目标')
    full_fm, _ = _front_matter_fields(full.read_text(encoding='utf-8-sig'))
    changes = {}
    zh = folder / f"{paper['citekey']}_ZH.md"
    if zh.is_file():
        zh_text = zh.read_text(encoding='utf-8-sig')
        zh_fm, _ = _front_matter_fields(zh_text)
        new_fm = dict(zh_fm)
        touched = []
        for field in ('zotero_key', 'doi', 'title', 'citekey'):
            if str(zh_fm.get(field, '')) != str(full_fm.get(field, '')):
                new_fm[field] = full_fm.get(field, '')
                touched.append({'field': field, 'old': str(zh_fm.get(field, ''))[:120], 'new': str(full_fm.get(field, ''))[:120]})
        if not str(zh_fm.get('source', '')).strip():
            new_fm['source'] = 'legacy-migrated by library-remedy (beta.15)'
            touched.append({'field': 'source', 'old': '', 'new': new_fm['source']})
        if touched:
            changes[zh.name] = {'file': zh.name, 'fields': touched, 'candidate': _rewrite_front_matter(zh_text, new_fm)}
    return changes

def _rewrite_front_matter(text, new_fm):
    header = re.compile('---\\r?\\n(.*?)\\r?\\n---\\r?\\n?', re.S)
    match = header.match(text)
    if not match:
        raise RuntimeError('文件缺少 front matter；不能做元数据迁移')
    inner = match.group(1)
    for field, value in new_fm.items():
        scalar = _yaml_scalar(value)
        pattern = re.compile('(?m)^(' + re.escape(field) + ':)[ \\t]*.*$')
        if pattern.search(inner):
            inner = pattern.sub(lambda m: m.group(1) + ' ' + scalar, inner, count=1)
        else:
            inner = inner.rstrip('\r\n') + '\n' + f'{field}: {scalar}'
    return text[:match.start(1)] + inner + text[match.end(1):]

def _yaml_scalar(value):
    text = str(value)
    special = ''.join((chr(c) for c in (58, 34, 39, 123, 125, 91, 93, 44, 38, 42, 63, 124, 62)))
    if not text or text != text.strip() or any((c in text for c in special)) or (text.lower() in ('true', 'false', 'null', 'yes', 'no')):
        return json.dumps(text, ensure_ascii=False)
    return text

def _migrate_remedy(client, runs, run_id, key, *, confirm):
    state, paper, found_run = _find_paper(runs, run_id, key)
    _identity_check(client, paper)
    folder = _paper_folder(paper['citekey'])
    initial = {'primary': s.sha256(Path(paper['primary_pdf'])), 'files': _folder_hashes(folder, paper['citekey'])}
    changes = _migrate_targets(paper, folder)
    result = {'action': 'migrate_metadata', 'key': key, 'citekey': paper['citekey'], 'changes': [{'file': row['file'], 'fields': row['fields']} for row in changes.values()], 'ambiguous_provenance': True}
    if not confirm:
        result['preview'] = True
        result['notes'] = ['仅确定性修复派生文件（ZH）的元数据字段对齐与来源标记；正文一字不改', '旧全文的 source_pdf 不凭当前 PDF 文件名改写；来源不明请人工核实', '候选先在临时目录过完整验收，通过才备份并原子替换；失败则原字节保持不变']
        return result
    if not changes:
        raise RuntimeError('没有可确定性迁移的元数据问题；请人工审阅')
    stage_root = s.CACHE / 'remedy_preview'
    stage_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=stage_root) as temporary:
        stage = Path(temporary) / folder.name
        shutil.copytree(folder, stage, ignore=shutil.ignore_patterns('.sync_*.lock'))
        for name, row in changes.items():
            (stage / name).write_text(row['candidate'], encoding='utf-8')
        from library_qa import validate_paper
        qa = validate_paper(stage, paper['citekey'], paper['primary_pdf'], require_qc=True, require_translation=_translation_required(paper))
        if qa.get('ok') is not True:
            codes = sorted({str(e.get('code')) for e in qa.get('errors', []) if e.get('code')})
            raise RuntimeError('迁移候选未通过完整验收（' + ','.join(codes) + '）；未写入任何字节，请人工审阅')
        _assert_live_unchanged(paper, folder, initial)
        published = _publish(stage, folder, paper['citekey'], 'migrate_metadata')
        backup = published['backup']
        working = copy.deepcopy(paper)
        working.pop('qa', None)
        try:
            s.validate_completed_paper(working)
            if working.get('qa', {}).get('ok') is not True:
                raise RuntimeError('迁移后完整验收未通过')
        except Exception as error:
            rolled = _rollback(published)
            raise RuntimeError('迁移后复验失败；' + ('已回滚为原字节' if rolled else '回滚未完成，请从备份恢复：' + str(backup))) from error
    _record_ledger(runs, key, action='migrate_metadata', status='complete', reasons=['旧版元数据已确定性迁移并通过完整验收'], release_hold=True, evidence=_fingerprints(working), run_report=state.get('report'), run_id=found_run)
    _mark_discovery_complete(runs, key, paper['citekey'], found_run)
    return {**result, 'ok': True, 'backup': str(backup), 'message': '迁移完成：候选通过完整验收后写入，原字节备份于 ' + str(backup)}

def _recheck_remedy(client, runs, run_id, key, *, confirm):
    from library_intake import plan_item
    state, paper, _ = _find_paper(runs, run_id, key)
    plan = plan_item(client, key)
    blocked = [str(x) for x in plan.get('blocked', [])]
    categories = classify_reasons(blocked, s.LIBRARY, plan.get('citekey', ''))
    names = {row['category'] for row in categories}
    qa_summary, qa_ok = ([], None)
    folder = _paper_folder(plan.get('citekey', ''))
    if folder is not None and folder.is_dir():
        from library_qa import validate_paper
        primary = paper.get('primary_pdf') if paper.get('primary_pdf') and Path(paper['primary_pdf']).is_file() else plan.get('primary_pdf', f"{plan.get('citekey', '')}.pdf")
        qa = validate_paper(folder, plan.get('citekey', ''), primary, require_qc=True, require_translation=_translation_required(paper))
        qa_ok = qa.get('ok') is True
        qa_summary = sorted({str(e.get('code')) for e in qa.get('errors', []) if e.get('code')})[:8]
    if blocked:
        status = 'missing_pdf' if 'missing_pdf' in names else 'needs_repair'
        reasons, release = (blocked, False)
        message = '复查仍未通过准备：' + '；'.join(blocked[:4])
    elif qa_ok is False:
        status, release = ('needs_repair', False)
        reasons = ['准备已通过，但产物完整验收未通过：' + ','.join(qa_summary)] if qa_summary else ['准备已通过，但产物完整验收未通过']
        message = reasons[0] + '。不能据此宣称已修好；可用预演/处置动作继续处理。'
    else:
        status, release = ('new', True)
        reasons = ['复查通过：准备核验通过' + ('、产物完整验收通过' if qa_ok else '（无既有产物可验收）') + '；条目回到可预演状态']
        message = '复查通过：该条目可重新预演（未写入任何内容；完成采纳需另行确认）。'
    if confirm:
        _record_ledger(runs, key, action='recheck', status=status, reasons=reasons, release_hold=release, run_id=run_id)
    return {'action': 'recheck', 'key': key, 'preview': not confirm, 'blocked': blocked[:8], 'classification': categories, 'qa_ok': qa_ok, 'qa_errors': qa_summary, 'ok': not blocked and qa_ok is not False, 'message': message if confirm else '复查结果（只读；未改动）：' + ('通过' if not blocked and qa_ok is not False else '未通过')}

def _execute_remedy(client, runs, run_id, key, action, params=None, *, confirm=False):
    if not isinstance(action, str) or not ACTION_RX.fullmatch(action):
        raise RuntimeError('未知处置动作：' + str(action)[:80])
    if action in WORKER_ACTIONS:
        if action == 'reparse_source' and (params or {}).get('explicit_fresh_parse') is not True and confirm:
            raise RuntimeError('全新解析必须显式选择（费用高）；请先在预览中勾选确认')
        return _worker_remedy(client, runs, run_id, key, action, params or {}, confirm=confirm)
    if action == 'adopt_existing':
        return _adopt_remedy(client, runs, run_id, key, confirm=confirm)
    if action == 'migrate_metadata':
        return _migrate_remedy(client, runs, run_id, key, confirm=confirm)
    if action == 'recheck':
        return _recheck_remedy(client, runs, run_id, key, confirm=confirm)
    if action in ('defer', 'exclude'):
        reason = str((params or {}).get('reason', '') or ('用户暂缓本篇' if action == 'defer' else '用户排除本篇'))
        if not confirm:
            return {'action': action, 'key': key, 'preview': True, 'notes': ['defer=暂缓并释放占用（之后可撤销）；exclude=自动发现不再选中（保留到你撤销）'], 'resulting_status': 'incomplete' if action == 'defer' else 'excluded'}
        row = _record_ledger(runs, key, action=action, run_id=run_id, status='incomplete' if action == 'defer' else 'excluded', reasons=[reason[:300]], release_hold=True)
        return {'action': action, 'key': key, 'ok': True, 'status': row.get('status'), 'message': '已记录处置（可撤销）。'}
    if action == 'undo':
        if not confirm:
            return {'action': 'undo', 'key': key, 'preview': True, 'notes': ['撤销最近一次显式处置，恢复其之前的状态']}
        ledger = load_issues(runs)
        undo_disposition(ledger, key)
        save_issues(runs, ledger)
        return {'action': 'undo', 'key': key, 'ok': True, 'status': ledger['items'][key].get('status'), 'message': '已撤销最近一次处置。'}
    raise RuntimeError('未知处置动作：' + str(action)[:80])

def remedy(client, runs, run_id, key, action, params=None, *, confirm=False, expected_binding=None):
    if not confirm:
        return _execute_remedy(client, runs, run_id, key, action, params, confirm=False)
    with s.workflow_lock():
        _guard_idle()
        if expected_binding is not None and binding(client, runs, run_id, key, action) != expected_binding:
            raise RuntimeError('预览已过期：输入在等待执行期间发生变化；请重新预览')
        _, paper, found_run = _find_paper(runs, run_id, key)
        from library_control import new_attempt
        attempt = new_attempt()
        record_path = Path(runs) / found_run / f'remedy_latest_{key}.json'
        attempt_dir = Path(runs) / attempt
        attempt_dir.mkdir(parents=True, exist_ok=False)
        record = {'run_id': attempt, 'source_run': found_run, 'key': key, 'action': action, 'status': 'running', 'started': dt.datetime.now().isoformat(timespec='seconds')}

        def persist():
            s.save_json(attempt_dir / 'remedy_state.json', record)
            s.save_json(record_path, record)
        persist()
        try:
            result = _execute_remedy(client, runs, run_id, key, action, params, confirm=True)
            record.update(status='completed' if result.get('ok') else 'failed', result=result, exit_code=0 if result.get('ok') else 1)
            if result.get('ok') and action in (*WORKER_ACTIONS, 'adopt_existing', 'migrate_metadata'):
                baseline = copy.deepcopy(paper)
                baseline.pop('qa', None)
                s.validate_completed_paper(baseline)
                if baseline.get('qa', {}).get('ok') is not True:
                    raise RuntimeError('处置结果记录前完整验收未通过；请复查当前文件')
                baseline.update(exit_code=0, progress_phase='validated', qc_log=[], blocked=[])
                record['qa'] = baseline['qa']
                s.save_json(Path(runs) / found_run / f'remedy_baseline_{key}.json', {'attempt': attempt, 'paper': baseline})
            return result
        except Exception as error:
            record.update(status='failed', exit_code=1, error=_clean(str(error), 1000))
            raise
        finally:
            record['finished'] = dt.datetime.now().isoformat(timespec='seconds')
            persist()
