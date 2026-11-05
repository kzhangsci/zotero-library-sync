# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import argparse
import concurrent.futures as cf
import contextlib
import csv
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import library_progress as progress
from library_power import system_awake
from common import REPO, REPORTS, CACHE
from library_paths import LIBRARY, GIT
PENDING = '待同步到 Obsidian'
RUNS = CACHE / 'library_sync'
PYTHON = Path(sys.executable)

def state_repo():
    return REPORTS.parent

def sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            break
        except PermissionError as error:
            if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 7:
                raise
            time.sleep(min(0.01 * 2 ** attempt, 0.2))

def progress_checkpoint(state, run):
    phase = state.get('finalize_phase') or state.get('phase', 'preflight')
    if state.get('display_phase') != phase:
        state['display_phase'] = phase
        state['phase_started'] = dt.datetime.now().isoformat(timespec='seconds')
    save_json(run / 'state.json', state)
    save_json(RUNS / 'latest.json', state)

def active_controller():
    return progress.active_run(RUNS)

def register_controller(run_id, pid=None):
    owner = progress.process_identity(os.getpid() if pid is None else pid)
    if owner is None:
        if pid is None:
            raise RuntimeError('无法登记当前控制器身份，未开始同步')
        return None
    owner.update(id=run_id, registered=dt.datetime.now().isoformat(timespec='seconds'))
    save_json(RUNS / run_id / 'controller.json', owner)
    save_json(RUNS / 'active.json', owner)
    return owner

@contextlib.contextmanager
def launch_lock():
    deadline = time.monotonic() + 10
    while True:
        lock = workflow_lock(RUNS / 'launch.lock')
        try:
            lock.__enter__()
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError('另一个入口正在启动任务；没有重复启动，请稍后再打开')
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)

@contextlib.contextmanager
def controller_lock(run_id):
    with contextlib.ExitStack() as stack:
        with launch_lock():
            owner = active_controller()
            if owner and (owner['pid'] != os.getpid() or owner['id'] != run_id):
                raise RuntimeError('已有同步任务运行；请打开 Library Sync 查看进度，不重复执行')
            stack.enter_context(workflow_lock())
            register_controller(run_id)
        stack.enter_context(system_awake())
        yield

def watch_progress(run_id):
    return progress.watch(RUNS, run_id, LIBRARY)

def git(repo, *args):
    p = subprocess.run([str(GIT or 'git'), '-C', str(repo), *args], capture_output=True)
    if p.returncode:
        raise RuntimeError(f'git {args[0]} failed: {p.stderr.decode('utf-8', 'replace')[:500]}')
    return p.stdout.decode('utf-8', 'strict')

def dirty_paths(repo):
    raw = git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split('\x00')
    result, i = (set(), 0)
    while i < len(raw):
        line = raw[i]
        i += 1
        if not line:
            continue
        result.add(line[3:])
        if 'R' in line[:2] or 'C' in line[:2]:
            result.add(raw[i])
            i += 1
    return result

@contextlib.contextmanager
def guarded_file(path, *, writable=False, create=False, movable=False):
    if os.name != 'nt':
        raise RuntimeError('Automatic publication/commit requires Windows file sharing guards')
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    access = 2147483648 | (1073741824 if writable else 0) | (65536 if movable else 0)
    handle = kernel.CreateFileW(str(Path(path).absolute()), access, 1, None, 1 if create else 3, 128, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if writable else os.O_RDONLY))
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'r+b' if writable else 'rb') as stream:
        yield stream

def assert_hashes(expected, label):
    for name, digest in expected.items():
        path = Path(name)
        actual = sha256(path) if path.is_file() else None
        if actual != digest or (digest is None and path.exists()):
            raise RuntimeError(f'{label} changed concurrently: {path}')

@contextlib.contextmanager
def freeze_files(expected):
    with contextlib.ExitStack() as stack:
        for name, digest in sorted(expected.items()):
            if digest is None:
                continue
            stream = stack.enter_context(guarded_file(name))
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise RuntimeError(f'QA/frozen input changed concurrently: {name}')
        assert_hashes(expected, 'QA/frozen input')
        yield
        assert_hashes(expected, 'QA/frozen input')

def scoped_commit(repo, paths, message, baseline_dirty=(), *, expected_hashes=None, guard=None):
    if git(repo, 'diff', '--cached', '--name-only', '-z'):
        raise RuntimeError('Git index contains pre-existing staged work; commit stopped without changing it')
    changed = dirty_paths(repo)
    allowed = sorted(set(paths) & changed - set(baseline_dirty))
    if not allowed:
        return None
    for path in allowed:
        resolved = (Path(repo) / path).resolve()
        if not resolved.is_relative_to(Path(repo).resolve()) or not resolved.is_file():
            raise RuntimeError(f'Refusing deletion/out-of-repository commit: {path}')
    approved = {}
    for path in allowed:
        name = str(Path(repo) / path)
        if expected_hashes is not None and name not in expected_hashes:
            raise RuntimeError(f'Missing approved hash for commit: {path}')
        approved[name] = expected_hashes[name] if expected_hashes is not None else sha256(name)
    with freeze_files(approved):
        if guard:
            guard()
        git(repo, 'add', '--', *allowed)
        staged = set(filter(None, git(repo, 'diff', '--cached', '--name-only', '-z').split('\x00')))
        if staged != set(allowed):
            raise RuntimeError('Git staging changed concurrently; automatic commit stopped')
        assert_hashes(approved, 'Commit input')
        if guard:
            guard()
        git(repo, 'commit', '--only', '-m', message, '--', *allowed)
    return git(repo, 'rev-parse', '--short', 'HEAD').strip()

@contextlib.contextmanager
def workflow_lock(path=None):
    import msvcrt
    path = Path(path or RUNS / 'workflow.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        if f.tell() == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise RuntimeError('另一条一键同步仍在运行；没有重复启动') from e
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

def active_workers():
    command = '@(Get-CimInstance Win32_Process -Filter "Name=\'python.exe\' OR Name=\'pythonw.exe\'" -ErrorAction Stop | Where-Object { $_.CommandLine -match \'(^|[\\\\/\\s"])(sync|sync_runner|intake_new)\\.py([\\s"]|$)\' } | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress'
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, encoding='utf-8', errors='replace', timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('无法核实其他同步进程，保守停止；请关闭旧 worker 后重试')
    data = json.loads(result.stdout.strip() or '[]')
    return data if isinstance(data, list) else [data]

def assert_idle():
    processes = active_workers()
    locks = list((LIBRARY / 'papers').glob('*/.sync_*.lock'))
    if processes or locks:
        raise RuntimeError(f'其他同步尚未结束：{len(processes)} 个进程 / {len(locks)} 个锁；未重启或终止它们')

def choose_items(client, keys='', since='', collection='', limit=0):
    if sum((bool(v) for v in (keys, since, collection))) > 1:
        raise ValueError('-Keys / -Since / -Collection 只能选择一种')
    if not any((keys, since, collection)):
        from library_discovery import discover
        return discover(client, RUNS, limit)['selected']
    if keys:
        tokens = [s.strip() for s in keys.split(',') if s.strip()]
        direct = [t for t in tokens if re.fullmatch('[A-Z0-9]{8}', t)]
        by_key = {k: client.get(f'items/{k}') for k in direct}
        remaining = set(tokens) - set(direct)
        if remaining:
            for item in client.list_all('items/top'):
                if item['data'].get('citationKey') in remaining:
                    if item['data']['citationKey'] in by_key:
                        raise RuntimeError('重复 citekey；不能确定目标')
                    by_key[item['data']['citationKey']] = item
        missing = [k for k in tokens if k not in by_key]
        if missing:
            raise RuntimeError('Zotero 中找不到：' + ', '.join(missing))
        selected = list({x['key']: x for x in by_key.values()}.values())
    elif since:
        dt.date.fromisoformat(since)
        selected = [x for x in client.list_all('items/top') if x['data'].get('dateAdded', '')[:10] >= since]
    else:
        cols = [x for x in client.list_all('collections') if x['data'].get('name') == collection]
        if len(cols) != 1:
            raise RuntimeError(f'需要唯一分类「{collection}」；首次请运行 tools/library_sync.ps1 -Setup -Apply')
        selected = client.list_all(f'collections/{cols[0]['key']}/items/top')
    selected = [x for x in selected if not x['data'].get('deleted') and x['data'].get('itemType') not in ('attachment', 'annotation', 'note')]
    selected.sort(key=lambda x: (x['data'].get('dateAdded', ''), x['key']))
    if limit:
        selected = selected[:limit]
    if len(selected) > 25:
        raise RuntimeError(f'命中 {len(selected)} 篇；请用 --keys/--limit 缩小到 25 篇以内')
    return [x['key'] for x in selected]

def translation_enabled(record):
    translate = record.get('translate', True)
    if type(translate) is not bool:
        raise ValueError('translate must be a bool')
    return translate

def paper_translation_required(paper):
    required = paper.get('require_translation', True)
    if type(required) is not bool:
        raise ValueError('require_translation must be a bool')
    return translation_enabled(paper) and required

def apply_translation_policy(paper, plan, parent=None):
    from sync import CJK, KANA, NO_TRANSLATE_LIST
    selected = plan.get('item_translation', {}).get(paper.get('key'), True)
    if type(selected) is not bool:
        raise ValueError('Per-paper translation selection must be a bool')
    translate = translation_enabled(plan) and selected
    required = paper_translation_required(paper)
    parent = parent if parent is not None else paper.get('parent', {})
    data = parent.get('data', parent)
    tags = {tag['tag'] for tag in data.get('tags', [])}
    language = (data.get('language') or '').strip().lower()
    title = data.get('title') or paper.get('title') or ''
    ck = paper.get('citekey', '')
    chinese = language.startswith('zh') or language in {'chinese', '中文', '汉语', 'cn'} or (bool(CJK.search(title)) and (not KANA.search(title)))
    skip = set()
    if NO_TRANSLATE_LIST.is_file():
        skip = {line.strip() for line in NO_TRANSLATE_LIST.read_text(encoding='utf-8').splitlines() if line.strip() and (not line.startswith('#'))}
    paper.update(translate=translate, require_translation=bool(translate and required and (not chinese) and ('no-translate' not in tags) and (ck not in skip)))

def model_roles(plan):
    translate = translation_enabled(plan)
    if 'items' in plan:
        translate = translate and any((paper_translation_required(p) for p in plan['items']))
    elif 'papers' in plan:
        translate = translate and any((paper_translation_required(p) for p in plan['papers']))
    return ('qc', 'zh') if translate else ('qc',)

def translation_mode(record):
    if not translation_enabled(record):
        return '源文模式（本次不翻译；保留 QC，已有 ZH 保留并完整验收）'
    if not paper_translation_required(record):
        return '源文模式（按中文或不译规则；保留 QC，已有 ZH 完整验收）'
    return '自动中译模式（仅为需要翻译且缺少 ZH 的文献生成中译）'

def _fulltext_lacks_qc(path):
    import yaml
    text = path.read_text(encoding='utf-8-sig')
    front = yaml.safe_load(text.split('---', 2)[1]) if text.startswith('---') else {}
    return not isinstance(front, dict) or not front.get('qc')

def ordinary_continue_receipts(paper, dirty, *, current_run_id=None):
    from library_refresh import continuation_receipts
    from library_source import find_previous_source
    ck = paper.get('citekey') or ''
    prefix = f'papers/{ck}/'
    fulltexts = [prefix + f'{ck}_{suffix}.md' for suffix in ('EN', 'MD')]
    gated = [x for x in fulltexts if x in dirty and (LIBRARY / x).is_file() and _fulltext_lacks_qc(LIBRARY / x)]
    if not ck or not gated:
        return ({}, [])
    receipts = continuation_receipts(find_previous_source(LIBRARY, RUNS, paper.get('parent', {}), include_run_receipts=True), item_key=paper.get('key'), source_sha256=paper.get('source_sha256'), folder=str(LIBRARY / 'papers' / ck), current_run_id=current_run_id)
    verified, unexplained = ({}, [])
    for rel in sorted((x for x in dirty if x.startswith(prefix))):
        path = LIBRARY / rel
        digest = (receipts or {}).get(os.path.normcase(str(path))) if path.is_file() else None
        if digest and digest == sha256(path):
            verified[rel] = digest
        else:
            unexplained.append(rel)
    return (verified, unexplained)

def make_plan(client, args):
    from library_intake import plan_item
    no_translate = getattr(args, 'no_translate', False)
    if type(no_translate) is not bool:
        raise ValueError('no_translate must be a bool')
    discovery = None
    if not any((args.keys, args.since, args.collection)):
        from library_discovery import discover
        discovery = discover(client, RUNS, args.limit)
        selected = discovery['selected']
    else:
        selected = choose_items(client, args.keys, args.since, args.collection, args.limit)
    refresh_source = getattr(args, 'refresh_source', False)
    if refresh_source and (not args.keys):
        raise ValueError('更换全文必须明确选择 Zotero 文献或 PDF，不能批量扫描历史文献')
    if refresh_source:
        from library_refresh import plan_refresh
        choices = getattr(args, 'primary_keys', {})
        items = [plan_refresh(client, key, primary_key=choices.get(key)) for key in selected]
    else:
        items = [plan_item(client, key, swap=args.swap) for key in selected]
    from library_control import held_items
    held = held_items(RUNS)
    for paper in items:
        if paper['key'] in held:
            paper.setdefault('blocked', []).append('已有暂停/恢复任务，请从任务记录继续：' + held[paper['key']])
    if not refresh_source:

        def _has_fulltext(paper):
            folder = LIBRARY / 'papers' / paper['citekey']
            return any(((folder / f'{paper['citekey']}_{suffix}.md').is_file() for suffix in ('EN', 'MD')))
        candidates = [p for p in items if not p.get('blocked') and p.get('citekey') and _has_fulltext(p)]
        if candidates:
            dirty = dirty_paths(LIBRARY)
            import yaml
            for paper in candidates:
                try:
                    verified, unexplained = ordinary_continue_receipts(paper, dirty)
                except (OSError, ValueError, yaml.YAMLError) as error:
                    paper.setdefault('blocked', []).append('文献目录有未提交原文但无法读取或解析（' + type(error).__name__ + ': ' + str(error)[:200] + '），保守暂缓；请人工检查或提交后重新预演：' + f'papers/{paper.get('citekey', '')}/。来源 PDF 未变；未改附件或文献')
                    continue
                if unexplained:
                    paper.setdefault('blocked', []).append('文献目录有未提交原文，且无法核实为本流水线上次未完成产物（需同条目、同来源 PDF 的独立历史运行收据且逐字节一致）；请人工审核或提交后重新预演：' + '; '.join(unexplained) + '。来源 PDF 未变；未改附件或文献')
                elif verified:
                    paper['source_continue_preview'] = {'same_source': True, 'verified_uncommitted': sorted(verified)}
                    paper.setdefault('note', '来源未变：复用原文/笔记，仅补质检与缺失中译；不重新解析、不归档、不生成重复附件')
    plan = {'schema': 1, 'created': dt.datetime.now().isoformat(timespec='seconds'), 'id': dt.datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6], 'items': items, 'swap': args.swap, 'workers': args.workers, 'chunk_parallel': args.chunk_parallel, 'commit': not args.no_commit, 'translate': not no_translate, 'refresh_source': refresh_source}
    for paper in items:
        apply_translation_policy(paper, plan)
    from model_api import load_config, validate_config
    configured = load_config()
    if configured is not None:
        plan['model_config'] = validate_config(configured, required_roles=model_roles(plan))
    if discovery is not None:
        plan['discovery'] = discovery
        plan['deferred'] = discovery['deferred'] + [p for p in items if p.get('blocked')]
        plan['items'] = [p for p in items if not p.get('blocked')]
    elif args.since:
        from library_discovery import load, revision
        ledger = load(RUNS, seed_since=args.since)
        plan['discovery'] = {'mode': 'explicit_date', 'since': ledger['since'], 'seed_since': args.since, 'revision': revision(RUNS), 'observed': items}
    if configured is not None and any((not p.get('blocked') for p in plan['items'])):
        from model_api import check_tongji_model_ids
        check_tongji_model_ids(plan['model_config'], model_roles(plan))
    return plan

def preview(plan):
    print(f'本次模式：{translation_mode(plan)}。')
    if plan.get('discovery'):
        print(f'自动新增起点（UTC）：{plan['discovery']['since']}；不改变任何 Zotero 分类。')
    print(f'命中 {len(plan['items'])} 篇；默认只预览。', flush=True)
    for p in plan['items']:
        print(f'\n{p['key']}  {p.get('citekey')}\n  {p.get('title', '')}', flush=True)
        print(f'  {translation_mode(p)}')
        if p.get('source_continue_preview'):
            print('  来源未变：复用原文/笔记，仅补质检与缺失中译；不重新解析、不归档、不生成重复附件。')
        print(f'  元数据：{p.get('metadata', {})}；附件动作：{len(p.get('actions', []))}')
        if p.get('metadata', {}).get('refresh'):
            print('  已审核源附件；BBT 刷新 citekey 后再生成并校验改名/两份制动作，当前不使用旧键构造目标路径。')
            print('  源附件角色：' + ', '.join((f'{k}={v['role']}' for k, v in p.get('roles', {}).items())))
        for action in p.get('actions', []):
            print(f'  - {action.get('action')} {action.get('oldName', '')} -> {action.get('newName', action.get('annotatedName', ''))}')
        for reason in p.get('blocked', []):
            print(f'  阻碍：{reason}')
        for warning in p.get('warnings', []):
            print(f'  注意：{warning}')
    for p in plan.get('deferred', []):
        print(f'\n保留待办 {p['key']} {p.get('citekey', '')}：{'；'.join(p.get('blocked', []))}')
    if plan.get('model_config'):
        for role, label in (('qc', '质检'), ('zh', '翻译')):
            if role not in model_roles(plan):
                continue
            profile = plan['model_config'][role]
            print(f'  {label} API：{profile['provider']} / {profile['model']}；推理={profile['reasoning_effort']}')
    translation_step = ' → 按文献规则中译' if translation_enabled(plan) else ' → 源文模式'
    print(f'\n确认执行后：附件整理 → MinerU → 模型质检{translation_step} → 自动验收 → 索引 → 选择性提交；结果记录在任务状态与报告，不发送桌面通知。')

def notify(title, message, report):
    return {'requested': False, 'desktop_popup': 'suppressed', 'mode': 'panel_only', 'title': title, 'message': message[:220], 'report': str(report)}

def index_child(library, output, args):
    if len(args) not in (4, 5) or args[:3] != ['--steps', 'indexes', '--snapshot'] or (len(args) == 5 and args[4] != '--apply'):
        raise ValueError('Isolated index child requires an explicit full snapshot and indexes-only args')
    library, output = (Path(library), Path(output))
    if output.resolve().is_relative_to(library.resolve()):
        raise ValueError('Index staging must be outside Library')
    import sync

    class IndexView:

        def __truediv__(self, name):
            if name == 'indexes':
                return output
            if name == 'papers':
                return library / name
            raise RuntimeError(f'Unexpected index-only Library access: {name}')
    old = (sync.LIB, sync.REPORTS, sys.argv)
    try:
        sync.LIB, sync.REPORTS = (IndexView(), output.parent)
        sys.argv = ['sync.py', *args]
        return sync.main()
    finally:
        sync.LIB, sync.REPORTS, sys.argv = old

def run_sync(args, logfile, env, *, index_output=None):
    logfile.parent.mkdir(parents=True, exist_ok=True)
    command = [str(PYTHON), '-B', '-X', 'utf8', str(REPO / 'tools' / 'sync.py'), *args]
    if index_output is not None:
        bootstrap = 'import sys; sys.path.insert(0, sys.argv.pop(1)); from library_sync import index_child; sys.exit(index_child(sys.argv[1], sys.argv[2], sys.argv[3:]))'
        command = [str(PYTHON), '-B', '-X', 'utf8', '-c', bootstrap, str(REPO / 'tools'), str(LIBRARY), str(index_output), *args]
    with logfile.open('w', encoding='utf-8') as log:
        proc = subprocess.Popen(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return proc.wait()

def fingerprint(paths):
    return {str(p): sha256(p) for p in paths if p.is_file()}

def paper_fingerprint(paper):
    folder = LIBRARY / 'papers' / paper['citekey']
    paths = [folder / f'{paper['citekey']}_{suffix}.md' for suffix in ('EN', 'MD', 'ZH', 'Note')]
    paths += [p for p in folder.rglob('*') if p.is_file() and (not p.name.startswith('.sync_'))]
    if any((not p.resolve().is_relative_to(folder.resolve()) for p in paths)):
        raise RuntimeError('Paper input escapes its folder')
    paths.append(Path(paper['primary_pdf']))
    return {str(p): sha256(p) if p.is_file() else None for p in paths}

def validate_completed_paper(paper):
    from library_qa import validate_paper
    paper['require_translation'] = paper_translation_required(paper)
    before = paper_fingerprint(paper)
    folder = LIBRARY / 'papers' / paper['citekey']
    paper['qa'] = validate_paper(folder, paper['citekey'], paper['primary_pdf'], require_qc=True, require_translation=paper.get('require_translation', True))
    paper['require_translation'] = paper['qa'].get('metrics', {}).get('translation_required', paper.get('require_translation', True))
    if paper['qa']['ok']:
        docs = {f'{paper['citekey']}_{suffix}.md': before[str(folder / f'{paper['citekey']}_{suffix}.md')] for suffix in ('EN', 'MD', 'ZH', 'Note') if before[str(folder / f'{paper['citekey']}_{suffix}.md')] is not None}
        if not docs or paper['qa']['metrics'].get('input_sha256') != docs:
            raise RuntimeError('QA input hashes missing or inconsistent; refusing completion')
        if paper_fingerprint(paper) != before:
            raise RuntimeError('QA inputs changed concurrently during validation')
        if before[str(Path(paper['primary_pdf']))] != paper['source_sha256']:
            raise RuntimeError('Primary PDF changed since intake')
    return before

def live_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('items'), list) or (not isinstance(snapshot.get('collections'), list)):
        raise ValueError('Invalid client snapshot')
    items = snapshot['items']
    deleted = {item['key'] for item in items if item['data'].get('deleted')}
    while True:
        extra = {item['key'] for item in items if item['data'].get('parentItem') in deleted} - deleted
        if not extra:
            break
        deleted.update(extra)
    return {**snapshot, 'items': [item for item in items if item['key'] not in deleted], 'collections': [c for c in snapshot['collections'] if not c['data'].get('deleted')]}

def pristine_note(path, item):
    import sync
    text = path.read_text(encoding='utf-8-sig')
    fm, _, _ = sync.front_matter(text)
    created = fm.get('created', '')
    try:
        dt.date.fromisoformat(created)
    except (TypeError, ValueError):
        return False
    d = item['data'] | {'_parsedDate': (item.get('meta') or {}).get('parsedDate') or ''}
    ck, key = (d['citationKey'], item['key'])
    header = sync.yaml_block({'citekey': ck, 'zotero_key': key, 'doi': d.get('DOI') or '', 'title': d.get('title') or '', 'year': sync.year_of(d), 'tags': ['paper'], 'created': created})
    expected = header + f'\n# {ck}\n\n' + sync.info_callout(d, ck, key) + '\n## 要点\n\n\n## 问题与想法\n\n\n## 关联文献\n\n'
    return text == expected

def index_fingerprint():
    return fingerprint((LIBRARY / 'indexes').rglob('*'))

def assert_indexes(expected):
    if index_fingerprint() != expected:
        raise RuntimeError('Library indexes changed concurrently; user changes preserved')

def publish_indexes(staged, baseline, backup):
    root = LIBRARY / 'indexes'
    assert_indexes(baseline)
    outputs = {str(root / p.relative_to(staged)): p.read_bytes() for p in staged.rglob('*') if p.is_file()}
    if not outputs:
        raise RuntimeError('Index generator produced no files')
    published = {**baseline, **{name: hashlib.sha256(data).hexdigest() for name, data in outputs.items()}}
    with contextlib.ExitStack() as stack:
        streams = {}
        for name, digest in sorted(baseline.items()):
            stream = stack.enter_context(guarded_file(name, writable=True))
            original = stream.read()
            if hashlib.sha256(original).hexdigest() != digest:
                raise RuntimeError(f'Library indexes changed concurrently: {name}')
            saved = backup / Path(name).relative_to(root)
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(original)
            streams[name] = stream
        assert_indexes(baseline)
        for name in sorted(outputs):
            path = Path(name)
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f'Index output escapes Library indexes: {name}')
            if name not in streams:
                path.parent.mkdir(parents=True, exist_ok=True)
                streams[name] = stack.enter_context(guarded_file(path, writable=True, create=True))
        for name, data in outputs.items():
            if baseline.get(name) == published[name]:
                continue
            stream = streams[name]
            stream.seek(0)
            stream.write(data)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        assert_indexes(published)
    return published

def report_text(state):
    lines = ['# 一键同步执行报告', '', f'- 任务：`{state['id']}`', f'- 状态：{state['status']}', f'- 开始：{state['started']}', f'- 结束：{state.get('finished', '')}', '- 模型：`bigmodel/glm-5.3-flash`（现有 max 推理配置；未绕过质检护栏）', f'- 分块并行：{state['chunk_parallel']}；全局 GLM 调用上限：2', '']
    if state.get('model_config'):
        configs = state['model_config']
        lines[6] = '- 模型（API，保留现有质检护栏）：' + '；'.join((f'{role}={configs[role]['provider']}/{configs[role]['model']} (effort={configs[role]['reasoning_effort']})' for role in model_roles(state)))
        lines[7] = f'- 分块并行：{state['chunk_parallel']}；全局模型调用上限：2'
    if state.get('worker_resume_of'):
        lines += [f'- 继续暂停任务：`{state['worker_resume_of']}`；只补缺失步骤，未重新整理附件。', '']
    if state.get('status') == 'paused':
        lines += ['- 已在步骤边界暂停，已完成产物与源文件哈希保存；控制器退出后可安全关机。', '']
    if state.get('resume_of'):
        lines[6:8] = [f'- 仅恢复收尾，原任务：`{state['resume_of']}`；未调用论文 worker、MinerU 或 GLM。', f'- 人工审定清单：`{state.get('reviewed_changes') or '无'}`', f'- 收尾阶段：{state.get('finalize_phase', 'preflight')}']
    if 'translate' in state:
        lines += [f'- 本次模式：{translation_mode(state)}', '']
    for item in state.get('papers', []):
        lines += [f'## {item.get('citekey', item.get('key'))}', '', f'- {translation_mode(item)}', '', '```json', json.dumps(item, ensure_ascii=False, indent=2), '```', '']
    if state.get('error'):
        lines += ['## 阻碍', '', state['error'], '']
    if state.get('deferred'):
        lines += ['## 未执行、保留待办', '']
        for item in state['deferred']:
            lines.append(f'- {item['key']} {item.get('citekey', '')}：{'；'.join(item.get('blocked', []))}')
        lines.append('')
    for warning in state.get('warnings', []):
        lines += [f'- 注意：{warning}']
    lines += ['', f'Library 提交：`{state.get('library_commit') or '无新提交'}`', '', '已有用户修改未提交；原始 PDF、批注和历史手稿未删除。', '全库快照读取不加文件锁；随后索引/提交期间，本批论文文件短暂只读。', '校验是确定性检查，不能替代对科学内容的人工审阅。']
    return '\n'.join(lines) + '\n'

def assert_unstaged():
    for repo in (LIBRARY, state_repo()):
        if git(repo, 'diff', '--cached', '--name-only', '-z'):
            raise RuntimeError('Git index contains pre-existing staged work; stopped before publication')

def previous_generated_paths(state, before, dirty, frozen, source_map):
    candidates = {}
    for paper in state['papers']:
        ck = paper['citekey']
        verified = paper.get('source_continue', {}).get('verified_uncommitted', [])
        if not paper.get('qa', {}).get('ok') or paper.get('source_refresh'):
            continue
        for suffix, step in (('Note', 'note'), ('EN', 'md'), ('MD', 'md'), ('ZH', 'zh')):
            rel = f'papers/{ck}/{ck}_{suffix}.md'
            path = LIBRARY / rel
            old_hash = before.get(str(path))
            if rel not in dirty or not old_hash or (not frozen.get(str(path))):
                continue
            if old_hash != frozen[str(path)] and (not (step == 'md' and rel in verified)):
                continue
            if step == 'note' and (not pristine_note(path, source_map[paper['key']])):
                continue
            candidates[rel] = (paper['key'], paper['source_sha256'], ck, old_hash, step)
    if not candidates:
        return set()
    from library_discovery import history, timestamp
    recovered = set()
    for old in history(RUNS):
        if old.get('id') == state['id'] or timestamp(old['started']) >= timestamp(state['started']) or old.get('status') not in ('blocked', 'attention', 'partial') or (old.get('monitor_done') is not True):
            continue
        baseline = old.get('baseline', {})
        if baseline.get('schema') != 1 or not isinstance(baseline.get('before'), dict) or (not isinstance(baseline.get('library_dirty'), list)):
            continue
        for item in old.get('papers', []):
            for rel, (key, source, ck, digest, step) in candidates.items():
                if (item.get('key'), item.get('source_sha256'), item.get('citekey')) != (key, source, ck):
                    continue
                path = LIBRARY / rel
                if str(path) not in baseline.get('owned', []) or str(path) in baseline['before'] or rel in baseline['library_dirty'] or item.get('source_refresh'):
                    continue
                hashes = (item.get('qa') or {}).get('metrics', {}).get('input_sha256', {})
                if hashes.get(path.name) != digest:
                    continue
                for line in item.get('qc_log', []):
                    if line.startswith('SUMMARY '):
                        try:
                            counts, _ = json.JSONDecoder().raw_decode(line[8:])
                        except ValueError:
                            continue
                        if isinstance(counts, dict) and counts.get(step) == 1:
                            recovered.add(rel)
    if recovered:
        tracked = git(LIBRARY, 'ls-tree', '-r', '--name-only', '-z', 'HEAD', '--', *sorted(recovered))
        recovered.difference_update(tracked.split('\x00'))
    return recovered

def finalize(client, state, run, *, frozen, before_index, before, owned, source_map, dirty, commit, env, checkpoint, verify_snapshot=None, extra_guard=None):
    snapfile = run / 'snapshot.json'

    def check_papers():
        current = {}
        for paper in state['papers']:
            current.update(paper_fingerprint(paper))
        if current != frozen:
            raise RuntimeError('QA inputs changed concurrently after validation')
        assert_idle()
        if extra_guard:
            extra_guard()
    check_papers()
    assert_indexes(before_index)
    recovered = previous_generated_paths(state, before, dirty, frozen, source_map) if commit else set()
    state['finalize_phase'] = 'snapshot'
    checkpoint()
    full_snapshot = live_snapshot(client.stable_snapshot())
    if verify_snapshot:
        verify_snapshot(full_snapshot)
    save_json(snapfile, full_snapshot)
    state['finalize_phase'] = 'index_stage'
    checkpoint()
    with freeze_files(frozen):
        check_papers()
        assert_indexes(before_index)
        staged = run / 'index_stage' / 'indexes'
        staged.mkdir(parents=True, exist_ok=False)
        for apply in (False, True):
            check_papers()
            assert_indexes(before_index)
            args = ['--steps', 'indexes', '--snapshot', str(snapfile)]
            if apply:
                args += ['--apply']
            if run_sync(args, run / ('indexes_apply.log' if apply else 'indexes_dryrun.log'), env, index_output=staged):
                raise RuntimeError('索引步骤失败')
            check_papers()
            assert_indexes(before_index)
        with (staged / 'status.csv').open(encoding='utf-8-sig', newline='') as f:
            records = list(csv.DictReader(f))
        rows = {r['citekey']: r for r in records}
        live_keys = {x['data']['citationKey'] for x in full_snapshot['items'] if x['data'].get('citationKey') and x['data'].get('itemType') not in ('attachment', 'annotation', 'note')}
        if set(rows) != live_keys or len(rows) != len(records):
            raise RuntimeError('Full-library index is incomplete/duplicate or contains non-live items')
        for p in state['papers']:
            row = rows.get(p['citekey'], {})
            fields = ['pdf', 'full', 'note'] + (['zh'] if paper_translation_required(p) else [])
            if any((str(row.get(k)).lower() not in ('true', '1') for k in fields)):
                raise RuntimeError(f'{p['citekey']} 索引状态不完整：{row}')
            p['index'] = {k: row.get(k) for k in fields}
        assert_unstaged()
        state['index_backup'] = str(run / 'index_backup')
        state['finalize_phase'] = 'publishing_indexes'
        checkpoint()
        published = publish_indexes(staged, before_index, Path(state['index_backup']))
        state['published_indexes'] = published
        state['finalize_phase'] = 'indexes_published'
        checkpoint()
        with freeze_files(published):

            def final_guard():
                check_papers()
                assert_indexes(published)
            final_guard()
            note_exclusions = set()
            for paper in state['papers']:
                note = LIBRARY / 'papers' / paper['citekey'] / f'{paper['citekey']}_Note.md'
                reanchored = paper.get('source_refresh', {}).get('reanchored', {})
                if str(note) in before and str(note) not in reanchored or (str(note) not in reanchored and (not pristine_note(note, source_map[paper['key']]))):
                    note_exclusions.add(note)
                    if str(note) not in before:
                        state['warnings'].append(f'{note.name} 不是空白标准骨架；保留用户内容，不自动提交')
            changed = [p.relative_to(LIBRARY).as_posix() for p in owned if frozen[str(p)] is not None and before.get(str(p)) != frozen[str(p)] and (p not in note_exclusions)]
            changed = sorted(set(changed) - set(dirty) | recovered)
            changed += [Path(name).relative_to(LIBRARY).as_posix() for name, digest in published.items() if before_index.get(name) != digest]
            state['finalize'] = {'paper_paths': [str(p) for p in owned], 'commit_paths': changed, 'recovered_paths': sorted(recovered)}
            if commit:
                state['finalize_phase'] = 'library_commit_started'
                checkpoint()
                state['library_commit'] = scoped_commit(LIBRARY, changed, '同步文献并校验 ' + ', '.join((p['citekey'] for p in state['papers'])), set(dirty) - recovered, expected_hashes={**frozen, **published}, guard=final_guard)
            state['finalize_phase'] = 'finalized'
            checkpoint()
            final_guard()
            state['status'] = 'completed'

def finish(state, run, report, *, commit, tools_dirty=(), discovery_ledger=None):
    state['finished'] = dt.datetime.now().isoformat(timespec='seconds')
    state['monitor_done'] = False
    if state['status'] == 'completed' and state['deferred']:
        state['workflow_complete'] = True
        state['status'] = 'partial'

    def persist_terminal():
        for path in (run / 'state.json', RUNS / 'latest.json'):
            try:
                save_json(path, state)
            except Exception as e:
                if state['status'] in ('completed', 'partial'):
                    state['status'] = 'attention'
                warning = f'状态保存失败 {path.name}：{e}'
                if warning not in state['warnings']:
                    state['warnings'].append(warning)
                print(warning, file=sys.stderr)

    def write_report():
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(report_text(state), encoding='utf-8')
            state['report_written'] = True
        except Exception as e:
            state['report_written'] = False
            if state['status'] in ('completed', 'partial'):
                state['status'] = 'attention'
            state['warnings'].append(f'报告写入失败；请查看任务 state.json/latest.json：{e}')
    persist_terminal()
    write_report()
    if state['status'] in ('completed', 'partial') and commit and state['report_written']:
        try:
            state['tools_commit'] = scoped_commit(state_repo(), [report.relative_to(state_repo()).as_posix()], '记录一键同步验收 ' + state['id'], tools_dirty)
        except Exception as e:
            state['status'] = 'attention'
            state['warnings'].append(f'报告未提交：{e}')
            write_report()
    if discovery_ledger is not None:
        try:
            from library_discovery import apply_result
            save_json(RUNS / 'discovery.json', apply_result(discovery_ledger, state))
        except Exception as e:
            state['status'] = 'attention'
            state['warnings'].append(f'待办保存失败：{e}；请保留本次 state.json 供恢复')
            write_report()
    persist_terminal()
    ok = state['status'] == 'completed'
    try:
        notice = notify('文献同步完成' if ok else '文献同步已暂停' if state['status'] == 'paused' else '文献同步需要处理', f'{len(state['papers'])} 篇；{state['status']}。详情：{report.name}', report)
        state['notification_requested'] = bool(notice.get('requested'))
        state['notification'] = notice
    except Exception as e:
        state['notification_requested'] = False
        state['warnings'].append(f'通知记录失败：{e}')
    state['monitor_done'] = True
    persist_terminal()
    print(f'{state['status']}: {report}', flush=True)
    return 0 if state['status'] == 'completed' else 2

def execute(client, plan):
    translation_enabled(plan)
    with controller_lock(plan['id']):
        return _execute_locked(client, plan)

def _execute_locked(client, plan):
    from library_intake import apply_item
    translate = translation_enabled(plan)
    from library_control import held_items
    held = held_items(RUNS)
    if any((p['key'] in held for p in plan['items'])):
        raise RuntimeError('所选文献已有暂停/恢复任务；请继续原任务，不重新准备附件')
    run = RUNS / plan['id']
    run.mkdir(parents=True, exist_ok=True)
    report = REPORTS / f'library_sync_{plan['id']}.md'
    state = {'id': plan['id'], 'started': dt.datetime.now().isoformat(timespec='seconds'), 'status': 'running', 'papers': [], 'warnings': [], 'chunk_parallel': plan['chunk_parallel'], 'report': str(report), 'requested': [], 'translate': translate, 'deferred': plan.get('deferred', []), 'monitor_done': False, 'controller': progress.process_identity(os.getpid()), 'phase': 'preflight', 'control_schema': 1, 'commit': plan['commit'], 'workers': plan['workers'], 'planned_papers': [{k: p.get(k, '') for k in ('key', 'citekey', 'title')} for p in plan['items']]}
    if plan.get('model_config'):
        from model_api import validate_config
        state['model_config'] = validate_config(plan['model_config'], required_roles=model_roles(plan))
    discovery_ledger = None
    tools_dirty = set()

    def checkpoint():
        progress_checkpoint(state, run)
    try:
        assert_idle()
        save_json(run / 'execution_plan.json', plan)
        if plan.get('model_config'):
            from model_api import check_tongji_model_ids, validate_config
            validate_config(plan['model_config'], require_credentials=True, required_roles=model_roles(plan))
            if plan['items'] and (not any((p.get('blocked') for p in plan['items']))):
                check_tongji_model_ids(state['model_config'], model_roles(plan))
        if plan.get('discovery'):
            from library_discovery import begin
            discovery_ledger = begin(RUNS, plan)
            save_json(RUNS / 'discovery.json', discovery_ledger)
        state['requested'] = [p['key'] for p in plan['items']]
        if not plan['items']:
            raise RuntimeError('没有可执行条目；原因已列出并保留待办，未调用 MinerU/GLM')
        blocked = [p for p in plan['items'] if p.get('blocked')]
        if blocked:
            raise RuntimeError('存在阻碍，整批未写入：' + '; '.join((str(p['blocked']) for p in blocked)))
        dirty = dirty_paths(LIBRARY)
        if any((p.startswith('indexes/') for p in dirty)):
            raise RuntimeError('indexes 有预存未提交修改，自动重建会覆盖它；请先检查')
        assert_unstaged()
        before_index = index_fingerprint()
        tools_dirty = dirty_paths(state_repo())
        checkpoint()
        refresh_before = {}
        for p in plan['items']:
            if p.get('refresh_source'):
                refresh_before.update(p['refresh']['files'])
        continue_verified = {}
        for p in plan['items']:
            if p.get('refresh_source'):
                from library_refresh import assert_refresh, same_source_receipts
                assert_refresh(p, client)
                old_folder = Path(p['refresh']['folder'])
                prefix = old_folder.relative_to(LIBRARY).as_posix() + '/'
                folder_dirty = sorted((x for x in dirty if x.startswith(prefix)))
                if folder_dirty:
                    receipts = same_source_receipts(p, current_run_id=plan.get('id'))
                    explained = {}
                    if receipts is not None:
                        for x in folder_dirty:
                            digest = receipts.get(os.path.normcase(str(LIBRARY / x)))
                            explained[x] = digest if digest and digest == sha256(LIBRARY / x) else None
                    if receipts is None:
                        raise RuntimeError('待更换文献有预存未提交改动，且本次更换会重建/归档这些文件，不能自动继续；请先人工审核或提交后重新预演：' + '; '.join(folder_dirty) + '。未改附件或文献')
                    unexplained = [x for x, digest in explained.items() if not digest]
                    if unexplained:
                        raise RuntimeError('待更换文献目录的未提交改动无法核实为本流水线上次产物（字节与运行回执不一致），请先人工审核或提交后重新预演：' + '; '.join(unexplained) + '。来源 PDF 未变；核实一致的产物只补缺失步骤，不重新解析。未改附件或文献')
                    continue_verified[p['key']] = dict(explained)
            else:
                verified, unexplained = ordinary_continue_receipts(p, dirty, current_run_id=plan.get('id'))
                if unexplained:
                    raise RuntimeError('待续跑文献目录的未提交改动无法核实为本流水线上次产物（字节与运行回执不一致），请先人工审核或提交后重新预演：' + '; '.join(unexplained) + '。来源 PDF 未变；核实一致的产物只补缺失步骤，不重新解析。未改附件或文献')
                if verified:
                    continue_verified[p['key']] = dict(verified)
        for p in plan['items']:
            state.update(phase='intake', current_key=p['key'])
            checkpoint()
            if p.get('refresh_source'):
                value = apply_item(client, p, swap=plan['swap'], allow_redo=True)
                if value.get('status') == 'applied' and (not value.get('blocked')):
                    from library_refresh import archive_generated
                    try:
                        value['source_refresh'] = archive_generated(p, value, run)
                    except Exception:
                        value['status'] = 'source_archive_blocked'
                        state['papers'].append(value)
                        checkpoint()
                        raise
                    value['redo_required'] = False
            else:
                value = apply_item(client, p, swap=plan['swap'])
            apply_translation_policy(value, plan, p.get('parent', {}))
            state['papers'].append(value)
            checkpoint()
            if value.get('status') != 'applied' or value.get('blocked') or value.get('redo_required'):
                raise RuntimeError(f'{p['key']} 附件/元数据未验收或需明确重跑：{value}')
            if value.get('key') != p['key'] or (not p.get('metadata', {}).get('refresh') and value.get('citekey') != p['citekey']):
                raise RuntimeError('Intake result does not match the approved item/citekey')
            if p['key'] in continue_verified:
                value['source_continue'] = {'same_source': True, 'verified_uncommitted': sorted(continue_verified[p['key']])}
        state.update(phase='worker_snapshot', current_key=None)
        checkpoint()
        scoped_items = []
        for paper in state['papers']:
            family = client.parent_children(paper['key'])
            scoped_items.extend([family['parent'], *family['children']])
        snapshot = live_snapshot({'time': dt.datetime.now().isoformat(timespec='seconds'), 'items': scoped_items, 'collections': client.list_all('collections')})
        snapfile = run / 'snapshot.json'
        save_json(snapfile, snapshot)
        source_map = {x['key']: x for x in snapshot['items']}
        for item in state['papers']:
            parent = source_map.get(item['key'], {}).get('data', {})
            if parent.get('citationKey') != item['citekey']:
                raise RuntimeError('快照缺失条目或键变化，未启动 worker')
            apply_translation_policy(item, plan, parent)
            ck = item['citekey']
            folder = LIBRARY / 'papers' / ck
            if any(folder.glob('.sync_*.lock')):
                raise RuntimeError(f'{ck} 有工作锁')
            for suffix in ('EN', 'MD'):
                source = folder / f'{ck}_{suffix}.md'
                rel = f'papers/{ck}/{ck}_{suffix}.md'
                if rel in dirty and source.exists():
                    verified = continue_verified.get(item['key'], {}).get(rel)
                    if verified and sha256(source) == verified:
                        continue
                    import yaml
                    text = source.read_text(encoding='utf-8-sig')
                    front = yaml.safe_load(text.split('---', 2)[1]) if text.startswith('---') else {}
                    if not isinstance(front, dict) or not front.get('qc'):
                        raise RuntimeError(f'{ck} 未质检原文有用户修改，不自动覆盖')
        owned = [LIBRARY / 'papers' / p['citekey'] / f'{p['citekey']}_{s}.md' for p in state['papers'] for s in ('EN', 'MD', 'ZH', 'Note')]
        before = fingerprint(owned)
        before.update({str(p): refresh_before[str(p)] for p in owned if str(p) in refresh_before})
        state['baseline'] = {'schema': 1, 'library_dirty': sorted(dirty), 'tools_dirty': sorted(tools_dirty), 'before': before, 'before_index': before_index, 'owned': [str(p) for p in owned]}
        checkpoint()
        frozen = {}
        env = os.environ.copy()
        env.update(PYTHONUTF8='1', QC_PARALLEL=str(plan['chunk_parallel']), ZH_PARALLEL=str(plan['chunk_parallel']), GLM_MAX_CONCURRENCY='2', LIBRARY_SYNC_PAUSE_FILE=str(run / 'pause_request.json'))
        model_args = ['--model', 'bigmodel/glm-5.3-flash']
        if plan.get('model_config'):
            public_models = run / 'model_config.json'
            save_json(public_models, state['model_config'])
            model_args = ['--api-config', str(public_models)]
        state['phase'] = 'workers'
        for p in state['papers']:
            p['log'] = str(run / (p['key'] + '.log'))
        checkpoint()
        with cf.ThreadPoolExecutor(max_workers=plan['workers']) as pool:
            futures = {}
            for p in state['papers']:
                args = ['--only', p['citekey'], '--snapshot', str(snapfile), '--apply', *model_args, '--worker-id', 'oneclick_' + p['key']]
                if p.get('source_refresh', {}).get('force_parse'):
                    args += ['--fresh-source', '--retry-failed']
                if not paper_translation_required(p):
                    args += ['--steps', 'folders,md,qc,note']
                logfile = run / (p['key'] + '.log')
                futures[pool.submit(run_sync, args, logfile, env)] = (p, logfile)
            for future in cf.as_completed(futures):
                p, logfile = futures[future]
                p['exit_code'] = future.result()
                p['log'] = str(logfile)
                text = logfile.read_text(encoding='utf-8', errors='replace')
                p['qc_log'] = [line for line in text.splitlines() if re.search('rejected|reject |chunk dropped|qc OK|qc done|qc:|accepted|SUMMARY|!!', line, re.I)]
                if p['exit_code'] == 75:
                    p['progress_phase'] = 'paused'
                    checkpoint()
                    continue
                p['progress_phase'] = 'qa'
                checkpoint()
                qa_before = validate_completed_paper(p)
                p['progress_phase'] = 'validated' if p['qa']['ok'] else 'failed'
                if p['qa']['ok']:
                    folder = LIBRARY / 'papers' / p['citekey']
                    note = str(folder / f'{p['citekey']}_Note.md')
                    expected_note = p.get('source_refresh', {}).get('reanchored', {}).get(note, before.get(note))
                    if note in before and expected_note != qa_before[note]:
                        raise RuntimeError('Existing user Note changed during workers; never auto-commit it')
                    if qa_before[str(Path(p['primary_pdf']))] != p['source_sha256']:
                        raise RuntimeError('Primary PDF changed since intake')
                    frozen.update(qa_before)
                    state['qa_frozen'] = dict(frozen)
                checkpoint()
        from library_control import seal_pause, finalize_barrier
        if not all((p.get('exit_code') in (0, 75) for p in state['papers'])):
            raise RuntimeError('worker 失败；未将失败伪装成暂停')
        bad = [p for p in state['papers'] if p['exit_code'] == 0 and (not p.get('qa', {}).get('ok'))]
        if bad:
            state['phase'] = 'qa'
            checkpoint()
            raise RuntimeError('产物验收未通过；未重建索引/提交；详见各篇 qa 和日志')
        if not finalize_barrier(state, run, checkpoint):
            seal_pause(state, run)
            return finish(state, run, report, commit=False, tools_dirty=tools_dirty, discovery_ledger=discovery_ledger)
        if any((p['exit_code'] for p in state['papers'])):
            raise RuntimeError('worker 暂停但缺少暂停请求；未重建索引/提交')
        finalize(client, state, run, frozen=frozen, before_index=before_index, before=before, owned=owned, source_map=source_map, dirty=dirty, commit=plan['commit'], env=env, checkpoint=checkpoint)
    except Exception as e:
        state['status'] = 'blocked'
        state['error'] = f'{type(e).__name__}: {e}'
    return finish(state, run, report, commit=plan['commit'], tools_dirty=tools_dirty, discovery_ledger=discovery_ledger)

def setup(client, apply=False):
    print('首次设置：复用官方本地 API 持久授权；不创建或改变分类。首次新增起点请用 -Since 日期确认。')
    if not apply:
        return 0
    from zotero_local import AuthorizationRequired
    try:
        client._require_key()
    except AuthorizationRequired:
        print('请选择 Zotero 对话框中的 Always Allow / 始终允许。')
        client.authorize()
    print('设置完成。以后 Zotero 保持运行即可；不需要 Run JavaScript 窗口。')
    return 0

def confirm_background(plan):
    translation_enabled(plan)
    if not plan['items']:
        print(f'暂无可执行条目；保留 {len(plan.get('deferred', []))} 篇待办。未启动后台任务，未写入确认计划或台账。', flush=True)
        return 2
    if any((p.get('blocked') for p in plan['items'])):
        print('预检未通过：请查看上述阻碍。未启动后台任务，未写入确认计划或台账。', flush=True)
        return 2
    try:
        answer = input('\n输入 YES 确认上述范围并在后台执行（不区分大小写）；其它输入取消：')
    except (EOFError, KeyboardInterrupt):
        answer = ''
    if answer.strip().casefold() != 'yes':
        print('\n已取消：未启动后台任务，未写入确认计划或台账。', flush=True)
        return 0
    run = RUNS / plan['id']
    path = run / 'confirmed_plan.json'
    logfile = run / 'controller.log'
    proc = None
    try:
        with launch_lock():
            existing = active_controller()
            if existing is None:
                save_json(path, plan)
                with logfile.open('w', encoding='utf-8') as log:
                    proc = subprocess.Popen([str(PYTHON), '-X', 'utf8', str(Path(__file__).resolve()), '--execute-plan', str(path), '--apply'], cwd=REPO, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                register_controller(plan['id'], proc.pid)
    except Exception as error:
        outcome = '未启动后台控制器' if proc is None else '已创建进程但登记失败；不要重复启动，请核查本次日志'
        print(f'后台启动失败：{type(error).__name__}: {error}；{outcome}。', file=sys.stderr, flush=True)
        return 2
    if existing:
        print('已有任务运行，转到该任务进度；没有启动重复任务。', flush=True)
        return watch_progress(existing['id'])
    try:
        code = proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        print(f'后台控制器已启动 PID {proc.pid}；日志：{logfile}。', flush=True)
        print('此提示仅确认进程启动，不代表同步完成；请用 tools/library_sync.ps1 -Status 查询进度。', flush=True)
        return watch_progress(plan['id'])
    print(f'后台控制器已在启动检查期间退出（退出码 {code}）；日志：{logfile}。', file=sys.stderr if code else sys.stdout, flush=True)
    print('请检查本次日志，并用 tools/library_sync.ps1 -Status 核实结果；不要把旧任务状态当作本次结果。', flush=True)
    return code

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    selectors = ap.add_mutually_exclusive_group()
    selectors.add_argument('--keys')
    selectors.add_argument('--since')
    selectors.add_argument('--collection', help='optional explicit collection; default discovers new items across collections')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, choices=range(1, 5), default=2)
    ap.add_argument('--chunk-parallel', type=int, choices=(1, 2), default=1)
    ap.add_argument('--swap', action='store_true', help='explicitly allow derived de-annotated primary; not a publisher original')
    ap.add_argument('--refresh-source', action='store_true', help='explicitly recheck selected full PDF; archive stale derived content before redo')
    ap.add_argument('--primary-key', help='select one full-PDF attachment (requires one --keys item and --refresh-source)')
    ap.add_argument('--no-commit', action='store_true')
    ap.add_argument('--no-translate', action='store_true', help='source-only for this run; retain QC and validate existing translations')
    ap.add_argument('--setup', action='store_true')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--watch', action='store_true', help='read-only live progress; never starts a worker')
    ap.add_argument('--interactive', action='store_true', help='preview, ask YES, then launch hidden controller')
    ap.add_argument('--execute-plan', type=Path, help=argparse.SUPPRESS)
    selectors.add_argument('--resume-finalize', metavar='RUN_ID')
    selectors.add_argument('--pause', metavar='RUN_ID', help='request a safe pause at the current step boundary')
    selectors.add_argument('--resume-run', metavar='RUN_ID', help='continue only unfinished work from a sealed pause')
    ap.add_argument('--attempt-id', help=argparse.SUPPRESS)
    ap.add_argument('--finalize-only', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--reviewed-changes', type=Path, metavar='PATH')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--apply', action='store_true')
    g.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    if args.refresh_source and (not args.keys or args.swap or args.resume_run or args.resume_finalize or args.execute_plan):
        ap.error('--refresh-source requires explicit --keys and cannot combine with swap/resume/execute-plan')
    args.primary_keys = {}
    if args.primary_key:
        if not args.refresh_source or not re.fullmatch('[A-Z0-9]{8}', args.primary_key) or (not re.fullmatch('[A-Z0-9]{8}', args.keys or '')):
            ap.error('--primary-key requires --refresh-source and exactly one Zotero item key')
        args.primary_keys[args.keys] = args.primary_key
    if (args.pause or args.resume_run) and any((args.setup, args.interactive, args.status, args.watch, args.execute_plan, args.swap, args.no_translate, args.no_commit, args.reviewed_changes, args.limit)):
        ap.error('Pause/resume uses the saved task; do not combine with another mode or configuration override')
    if (args.attempt_id or args.finalize_only) and (not args.resume_run):
        ap.error('--attempt-id/--finalize-only require --resume-run')
    if args.pause:
        from library_control import request_pause
        if args.apply:
            print(json.dumps(request_pause(args.pause), ensure_ascii=False))
        else:
            print(progress.render(progress.snapshot(RUNS, args.pause, LIBRARY)))
            print('仅查看；加 --apply 请求在步骤边界暂停。')
        return 0
    if args.resume_run:
        from library_control import resume_run
        from zotero_local import Client
        return resume_run(Client(apply=False), args.resume_run, apply=args.apply, attempt=args.attempt_id, finalize_only=args.finalize_only)
    if args.reviewed_changes and (not args.resume_finalize):
        ap.error('--reviewed-changes requires --resume-finalize')
    if args.resume_finalize and any((args.keys, args.since, args.collection, args.limit, args.setup, args.interactive, args.status, args.execute_plan, args.swap, args.no_commit, args.no_translate)):
        ap.error('--resume-finalize conflicts with selectors, Setup/Interactive/Status, --execute-plan, --swap, --no-commit and --no-translate (resume uses frozen policy)')
    if args.execute_plan and args.no_translate:
        ap.error('--execute-plan uses its frozen translation policy; do not override with --no-translate')
    if args.limit < 0:
        ap.error('--limit must be non-negative')
    if (args.status or args.watch) and any((args.keys, args.since, args.collection, args.setup, args.interactive, args.execute_plan, args.apply, args.resume_finalize)):
        ap.error('--status/--watch are read-only and cannot be combined with execution selectors')
    if args.status or args.watch:
        owner = active_controller()
        latest = RUNS / 'latest.json'
        run_id = owner['id'] if owner else json.loads(latest.read_text(encoding='utf-8'))['id'] if latest.exists() else None
        if run_id:
            if args.watch:
                return watch_progress(run_id)
            print(progress.render(progress.snapshot(RUNS, run_id, LIBRARY)))
        else:
            print('尚无一键同步任务')
        return 0
    if args.interactive and (not args.dry_run):
        owner = active_controller()
        if owner:
            print('已有任务运行，只显示进度，不重复启动。', flush=True)
            return watch_progress(owner['id'])
    from zotero_local import Client
    if args.resume_finalize:
        logger = logging.getLogger('zotero_local')
        if not any((getattr(h, 'library_progress', False) for h in logger.handlers)):
            handler = logging.StreamHandler()
            handler.library_progress = True
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    client = Client(apply=False if args.resume_finalize else args.apply or args.interactive)
    if args.resume_finalize:
        from library_resume import resume_finalize
        return resume_finalize(client, args.resume_finalize, apply=args.apply, reviewed_changes=args.reviewed_changes)
    if args.setup:
        return setup(client, args.apply)
    if args.execute_plan:
        if not args.apply or not args.execute_plan.resolve().is_relative_to(RUNS.resolve()):
            raise RuntimeError('执行计划须来自本工具缓存且显式 --apply')
        plan = json.loads(args.execute_plan.read_text(encoding='utf-8'))
        translation_enabled(plan)
        if time.time() - args.execute_plan.stat().st_mtime > 600:
            raise RuntimeError('预览超过 10 分钟，请重新预览')
    else:
        assert_idle()
        if args.interactive:
            print('正在检查新增及未完成文献、核对附件，请稍候；尚未开始同步。', flush=True)
        plan = make_plan(client, args)
        preview(plan)
    if not plan['items'] and (not plan.get('deferred')):
        print('没有命中条目，无需同步。')
        if args.interactive and (not args.apply):
            print('未启动后台任务，未写入确认计划或台账。', flush=True)
        return 0
    if args.dry_run:
        if args.interactive:
            print('仅预览：未启动后台任务，未写入确认计划或台账。', flush=True)
        return 0
    if args.interactive and (not args.apply):
        return confirm_background(plan)
    return execute(client, plan) if args.apply else 0
if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'停止：{type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(2)
