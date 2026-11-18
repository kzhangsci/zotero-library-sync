# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import argparse
import concurrent.futures
import contextlib
import copy
import datetime as dt
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
import library_sync as workflow
import library_progress as progress
from glm_concurrency import FileLock
from common import REPO
from library_paths import LIBRARY, SECRETS
MANIFEST = SECRETS / 'plugin_bridge.json'
MAX_BODY = 65536
PREVIEW_TTL = 600
CATALOG_TTL = 600.0
CATALOG_QUEUE_LIMIT = 8
CATALOG_CACHE_LIMIT = 8
CAPABILITIES = ['keys-v1', 'mineru-v1', 'translation-policy-v1', 'credential-reveal-v1', 'model-choices-v1', 'model-profiles-v2', 'model-test-role-v1', 'pause-resume-v1', 'history-v1', 'item-selection-v1', 'source-refresh-v1', 'official-model-catalog-v1', 'catalog-cache-v1', 'tongji-api-v1', 'preview-rebind-v1', 'translation-repair-v1', 'report-retention-v1', 'zcode-plans-v1', 'pause-recovery-v1', 'resume-model-choice-v1', 'diagnosis-v1', 'remedy-v1', 'issues-ledger-v1', 'acronyms-v1']
REVEAL_ERROR = '无法显示密钥；请检查已保存凭据并明确确认显示。'

class RequestError(ValueError):
    pass

def object_fields(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise RequestError('请求字段不受支持')
    return value

def parse_json(value):
    return json.loads(value, object_pairs_hook=progress._unique)

def safe_error(error):
    from zotero_local import LocalAPIError
    if isinstance(error, (RequestError, LocalAPIError, progress.ProgressError)):
        return str(error)[:1000]
    if type(error).__module__ in ('model_api', 'mineru_credentials'):
        return str(error)[:1000]
    if isinstance(error, RuntimeError):
        return str(error)[:1000]
    return f'操作失败（{type(error).__name__}）；没有自动重试或切换服务商。请检查配置与任务报告。'

def preview_text(plan):
    deferred = plan.get('deferred', [])
    blocked = [item for item in plan['items'] if item.get('blocked')]
    ready = len(plan['items']) - len(blocked)
    lines = [f"发现 {len(plan['items']) + len(deferred)} 篇：可执行 {ready} 篇、暂缓 {len(deferred) + len(blocked)} 篇；这里只预览，尚未写入 Zotero 或启动同步。"]
    for role, label in (('qc', '质检'), ('zh', '翻译')):
        if role == 'zh' and (not plan.get('translate', True)):
            lines.append('翻译：本次关闭；保留已有译文，不改变 Zotero 标签。')
            continue
        p = plan['model_config'][role]
        plan_label = f"，订阅={p['plan']}" if p.get('provider') == 'zcode' and p.get('plan') else ''
        lines.append(f"{label}：{p['provider']} / {p['model']}，推理强度={p['reasoning_effort']}{plan_label}，Thinking={p.get('thinking', 'default')}，输出上限={p.get('max_tokens', '默认')}，单次超时={p.get('timeout', '默认')} 秒")
    for item in plan['items']:
        lines += ['', f"{item['key']}  {item.get('citekey', '')}", str(item.get('title', '')), '元数据：' + json.dumps(item.get('metadata', {}), ensure_ascii=False)]
        if item.get('refresh_source'):
            lines.append('全文更新：' + item.get('note', '重新检测正本及旧来源'))
        elif item.get('source_continue_preview'):
            lines.append('来源未变：' + item.get('note', '复用原文/笔记，仅补质检与缺失中译'))
        if item.get('pdf_check'):
            checked = item['pdf_check']
            expected = checked.get('metrics', {}).get('pages_metadata', {}).get('expected_count')
            lines.append(f"PDF 检查：实际 {checked.get('page_count', '未知')} 页" + (f'；条目页码范围对应 {expected} 页' if expected else '；条目没有可比较的页码范围') + ('；疑似缺页，请核对全文' if checked.get('likely_incomplete') else '；未发现明确缺页证据（不保证完整）'))
        for action in item.get('actions', []):
            lines.append('附件动作：' + json.dumps(action, ensure_ascii=False))
        lines.extend(('阻碍：' + str(x) for x in item.get('blocked', [])))
        lines.extend(('注意：' + str(x) for x in item.get('warnings', [])))
        if item.get('require_translation') is False:
            lines.append('本篇：只要求原文与质检，不新增译文（本次设置、中文或已有控制规则）。')
    for item in deferred:
        lines.append(f"保留待办 {item['key']}：{item.get('title', '')[:80]}；" + '; '.join(item.get('blocked', [])))
    translation = '翻译（中文自动跳过） → ' if plan.get('translate', True) else '跳过翻译 → '
    lines += ['', '确认后：正本命名与批注副本 → MinerU → 质检 → ' + translation + '校验 → 索引 → 选择性 Git；结果在插件任务页与报告显示，不发送桌面通知。', 'PDF 将交给 MinerU；待处理文本将发送到上面所选 API 服务商。关闭面板不影响后台，请保持 Zotero 运行。']
    return '\n'.join(lines)

def artifact_summary(item):
    citekey = item.get('citekey')
    if not isinstance(citekey, str) or not citekey or any((c in citekey for c in '\\"/:*?<>|')) or ('..' in citekey):
        return {'files': [], 'qc_marked': False}
    folder = LIBRARY / 'papers' / citekey
    try:
        files = [suffix for suffix in ('EN', 'MD', 'ZH', 'Note') if (folder / f'{citekey}_{suffix}.md').is_file()]
        qc = False
        for suffix in ('EN', 'MD'):
            path = folder / f'{citekey}_{suffix}.md'
            if path.is_file():
                try:
                    qc = not workflow._fulltext_lacks_qc(path)
                except Exception:
                    qc = False
                if qc:
                    break
        return {'files': files, 'qc_marked': qc}
    except OSError:
        return {'files': [], 'qc_marked': False}

def public_items(plan):
    items = []
    for item in plan['items']:
        row = copy.deepcopy(item)
        row['artifacts'] = artifact_summary(item)
        items.append(row)
    return items

def preview_response(plan):
    return {'preview_id': plan['id'], 'text': preview_text(plan), 'items': public_items(plan), 'deferred': copy.deepcopy(plan.get('deferred', [])), 'model_config': copy.deepcopy(plan.get('model_config'))}

def launch_plan(plan):
    with workflow.launch_lock():
        owner = workflow.active_controller()
        if owner:
            return {'run_id': owner['id'], 'started': False, 'joined': True}
        run = workflow.RUNS / plan['id']
        path = run / 'confirmed_plan.json'
        if path.exists():
            return {'run_id': plan['id'], 'started': False, 'already_submitted': True}
        workflow.assert_idle()
        workflow.save_json(path, plan)
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        with (run / 'controller.log').open('w', encoding='utf-8') as log:
            proc = subprocess.Popen([str(python), '-X', 'utf8', str(REPO / 'tools' / 'library_sync.py'), '--execute-plan', str(path), '--apply'], cwd=REPO, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        workflow.register_controller(plan['id'], proc.pid)
    return {'run_id': plan['id'], 'started': True, 'pid': proc.pid, 'message': '仅确认后台进程启动；以最终 QA、索引、Git 与报告状态为准。'}

class Service:

    def __init__(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.catalog_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.lock = threading.RLock()
        self.operations = {}
        self.operation_kinds = {}
        self.catalog_merge = {}
        self.catalog_cache = {}
        self.catalog_generation = 0
        self.previews = {}
        self.rebound = {}
        self.remedy_previews = {}
        self.last_access = time.monotonic()

    def _prune(self):
        if len(self.operations) < 64:
            return
        for oid, future in list(self.operations.items()):
            if not future.done():
                continue
            del self.operations[oid]
            self.operation_kinds.pop(oid, None)
            self.catalog_merge = {key: value for key, value in self.catalog_merge.items() if value[0] != oid}
            if len(self.operations) < 64:
                break

    def submit(self, fn, *, catalog=False, merge_key=None, refresh=True):
        with self.lock:
            self._prune()
            if catalog:
                for key, (oid, refresh_kind) in list(self.catalog_merge.items()):
                    future = self.operations.get(oid)
                    if future is None or future.done():
                        del self.catalog_merge[key]
                if merge_key is not None and merge_key in self.catalog_merge:
                    oid, kind = self.catalog_merge[merge_key]
                    if kind or not refresh:
                        return {'operation': oid, 'merged': True}
                pending = [f for oid, f in self.operations.items() if not f.done() and self.operation_kinds.get(oid) == 'catalog']
                if len(pending) >= CATALOG_QUEUE_LIMIT:
                    raise RequestError('模型列表请求过多，请稍后重试或手动刷新')
                oid = str(uuid.uuid4())
                self.operations[oid] = self.catalog_pool.submit(fn)
                self.operation_kinds[oid] = 'catalog'
                if merge_key is not None:
                    self.catalog_merge[merge_key] = (oid, bool(refresh))
                return {'operation': oid}
            if any((not f.done() for oid, f in self.operations.items() if self.operation_kinds.get(oid) != 'catalog')):
                raise RequestError('已有预检或连接测试进行中，请等待其结果')
            oid = str(uuid.uuid4())
            self.operations[oid] = self.pool.submit(fn)
            self.operation_kinds[oid] = 'exclusive'
            return {'operation': oid}

    def _catalog_key(self, selected):
        return json.dumps({name: selected.get(name) for name in ('provider', 'base_url', 'models_url', 'protocol', 'endpoint_confirmed')}, sort_keys=True, separators=(',', ':'), ensure_ascii=True)

    def fetch_catalog(self, selected, *, refresh):
        from model_api import model_choices
        key = self._catalog_key(selected)
        with self.lock:
            cached = self.catalog_cache.get(key)
            if cached is not None and cached['generation'] == self.catalog_generation and (time.monotonic() - cached['at'] < CATALOG_TTL):
                if not refresh:
                    return {**copy.deepcopy(cached['result']), 'cache': 'hit'}
        try:
            result = model_choices(profile=copy.deepcopy(selected))
        except Exception:
            with self.lock:
                cached = self.catalog_cache.get(key)
            if cached is not None and cached['generation'] == self.catalog_generation:
                stale = copy.deepcopy(cached['result'])
                return {**stale, 'cache': 'stale', 'notice': stale.get('notice', '') + ' 本次刷新失败，保留上次候选（缓存，非实时）。'}
            raise
        with self.lock:
            if len(self.catalog_cache) >= CATALOG_CACHE_LIMIT and key not in self.catalog_cache:
                self.catalog_cache.pop(next(iter(self.catalog_cache)), None)
            self.catalog_cache[key] = {'at': time.monotonic(), 'generation': self.catalog_generation, 'result': copy.deepcopy(result)}
        return result

    def get_operation(self, oid):
        if not re.fullmatch('[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', oid):
            raise RequestError('操作 id 无效')
        with self.lock:
            future = self.operations.get(oid)
        if future is None:
            raise RequestError('操作不存在或已过期，请重新预览')
        if not future.done():
            return {'status': 'running'}
        try:
            return {'status': 'completed', 'result': future.result()}
        except Exception as error:
            return {'status': 'failed', 'error': safe_error(error)}

    def status(self, selected_run=None):
        owner = workflow.active_controller()
        latest = progress._read_json(workflow.RUNS / 'latest.json')
        run_id = selected_run or (owner['id'] if owner else (latest or {}).get('id'))
        if run_id is None:
            return {'active': False, 'view': None, 'text': '尚无同步任务。打开面板不会启动同步。', 'capabilities': CAPABILITIES}
        view = progress.snapshot(workflow.RUNS, run_id, workflow.LIBRARY)
        submitted = workflow.RUNS / run_id / 'resume_dispatch.json'
        if submitted.is_file():
            dispatch = progress._read_json(submitted)
            view.update(can_resume=False, can_resume_finalize=False, resumed_as=dispatch.get('run_id'))
        return {'active': bool(owner), 'view': view, 'text': progress.render(view), 'capabilities': CAPABILITIES, 'historical': bool(selected_run and (not owner or owner['id'] != selected_run))}

    def history(self):
        runs = []
        paths = sorted(workflow.RUNS.glob('*/state.json'), reverse=True)
        for path in paths[:40]:
            try:
                runs.append(self.status(path.parent.name)['view'])
            except (progress.ProgressError, OSError):
                runs.append({'id': path.parent.name, 'status': 'attention', 'status_text': '历史记录暂不可读', 'can_pause': False, 'can_resume': False, 'can_resume_finalize': False})
        return {'runs': runs, 'capabilities': CAPABILITIES}

    def make_preview(self, payload):
        from model_api import load_config, validate_config, assert_callable_routes
        from zotero_local import Client
        object_fields(payload, {'keys', 'workers', 'chunk_parallel', 'limit', 'translate', 'refresh_source', 'primary_keys'})
        translate = payload.get('translate', True)
        if type(translate) is not bool:
            raise RequestError('翻译选项必须为布尔值')
        keys = payload.get('keys', [])
        if not isinstance(keys, list) or len(keys) > 25 or any((not isinstance(k, str) or not re.fullmatch('[A-Z0-9]{8}', k) for k in keys)):
            raise RequestError('请选择最多 25 条普通文献，或检测新增')
        refresh = payload.get('refresh_source', False)
        primary_keys = payload.get('primary_keys', {})
        if type(refresh) is not bool or not isinstance(primary_keys, dict) or (refresh and (not keys)) or (primary_keys and (not refresh)) or any((k not in keys or not isinstance(v, str) or (not re.fullmatch('[A-Z0-9]{8}', v)) for k, v in primary_keys.items())):
            raise RequestError('更换全文须明确选择文献及合法 PDF 附件，不能用于自动发现')
        workers, parallel, limit = (payload.get('workers', 2), payload.get('chunk_parallel', 1), payload.get('limit', 0))
        if type(workers) is not int or workers not in range(1, 5) or type(parallel) is not int or (parallel not in (1, 2)) or (type(limit) is not int) or (not 0 <= limit <= 25):
            raise RequestError('并行数或条目数无效')
        if workflow.active_controller():
            raise RequestError('已有同步任务；请在进度页查看，不重复启动')
        configured = load_config()
        if configured is None:
            raise RequestError('请先在设置页保存 GLM / DeepSeek / 同济校内 API 配置与密钥')
        config = validate_config(configured)
        assert_callable_routes(config, ('qc', 'zh') if translate else ('qc',))
        workflow.assert_idle()
        args = types.SimpleNamespace(keys=','.join(dict.fromkeys(keys)), since='', collection='', limit=limit, workers=workers, chunk_parallel=parallel, no_commit=False, swap=False, no_translate=not translate, refresh_source=refresh, primary_keys=primary_keys)
        plan = workflow.make_plan(Client(apply=False), args)
        roles = ('qc', 'zh') if any((p.get('require_translation', True) for p in plan['items'])) else ('qc',)
        validate_config(config, require_credentials=True, required_roles=roles)
        if plan.get('model_config') != config:
            raise RequestError('模型配置在预检时变化，请重新预览')
        with self.lock:
            self.previews = {key: value for key, value in self.previews.items() if time.monotonic() - value[0] < PREVIEW_TTL}
            self.rebound = {old: new for old, new in self.rebound.items() if new in self.previews}
            self.previews[plan['id']] = (time.monotonic(), copy.deepcopy(plan))
        return preview_response(plan)

    def _fresh_preview_id(self):
        while True:
            candidate = dt.datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6]
            if candidate not in self.previews and (not (workflow.RUNS / candidate).exists()):
                return candidate

    def rebind_preview(self, payload):
        from model_api import load_config, validate_config
        object_fields(payload, {'preview_id', 'confirm'})
        if payload.get('confirm') is not True:
            raise RequestError('尚未明确确认，不会更新预演模型')
        run_id = payload.get('preview_id')
        progress._run_id(run_id)
        with self.lock, workflow.launch_lock():
            seen = set()
            current = run_id
            while current in self.rebound and current not in seen:
                seen.add(current)
                current = self.rebound[current]
            value = self.previews.get(current)
            if value is None or time.monotonic() - value[0] > PREVIEW_TTL:
                raise RequestError('预览已过期，请重新预演；不会自动重试旧任务')
            if workflow.active_controller():
                raise RequestError('已有同步任务；请在任务结束后更新预演模型')
            if (workflow.RUNS / current / 'confirmed_plan.json').exists():
                raise RequestError('该预演已提交或已有运行，不能更新模型；请重新预演')
            plan = value[1]
            if not plan.get('model_config'):
                raise RequestError('该预演没有模型绑定，请重新预演')
            roles = ('qc', 'zh') if any((p.get('require_translation', True) for p in plan['items'])) else ('qc',)
            config = validate_config(load_config(), require_credentials=True, required_roles=roles)
            if config == plan['model_config']:
                result = preview_response(plan)
                result['rebound'] = True
                result['unchanged'] = True
                return result
            new_id = self._fresh_preview_id()
            rebound = copy.deepcopy(plan)
            rebound['id'] = new_id
            rebound['model_config'] = config
            self.previews[new_id] = (value[0], rebound)
            del self.previews[current]
            if current != run_id:
                self.rebound[run_id] = new_id
            self.rebound[current] = new_id
            self.rebound = {old: new for old, new in self.rebound.items() if new in self.previews}
            result = preview_response(rebound)
            result['rebound'] = True
            return result

    def start(self, payload):
        from model_api import load_config, validate_config
        object_fields(payload, {'preview_id', 'confirm', 'selections'})
        if payload.get('confirm') is not True:
            raise RequestError('尚未明确确认，不会启动任务')
        run_id = payload.get('preview_id')
        progress._run_id(run_id)
        with self.lock:
            value = self.previews.get(run_id)
            if value is None or time.monotonic() - value[0] > PREVIEW_TTL:
                raise RequestError('预览已过期，请重新预览；不会自动重试旧任务')
            plan = copy.deepcopy(value[1])
            selections = payload.get('selections')
            if selections is not None:
                expected = {p['key'] for p in plan['items']}
                if not isinstance(selections, list) or len(selections) != len(expected):
                    raise RequestError('逐篇选择必须完整对应本次预演')
                by_key = {}
                for selection in selections:
                    object_fields(selection, {'key', 'include', 'translate'})
                    if set(selection) != {'key', 'include', 'translate'} or type(selection['key']) is not str or selection['key'] not in expected or (selection['key'] in by_key) or (type(selection['include']) is not bool) or (type(selection['translate']) is not bool):
                        raise RequestError('逐篇选择内容无效或条目重复')
                    by_key[selection['key']] = selection
                omitted = [p for p in plan['items'] if not by_key[p['key']]['include']]
                plan['deferred'] = plan.get('deferred', []) + [{'key': p['key'], 'citekey': p.get('citekey', ''), 'blocked': ['本次未勾选，保留待办']} for p in omitted]
                plan['items'] = [p for p in plan['items'] if by_key[p['key']]['include']]
                plan['item_translation'] = {p['key']: by_key[p['key']]['translate'] for p in plan['items']}
                for paper in plan['items']:
                    workflow.apply_translation_policy(paper, plan)
            if not plan['items'] or any((p.get('blocked') for p in plan['items'])):
                raise RequestError('预检没有可执行条目或存在阻碍，未启动')
            roles = ('qc', 'zh') if any((p.get('require_translation', True) for p in plan['items'])) else ('qc',)
            config = validate_config(load_config(), require_credentials=True, required_roles=roles)
            if config != plan['model_config']:
                raise RequestError('模型配置已变化，请重新预览后确认')
            return launch_plan(plan)

    def save_acronym(self, payload):
        import csv as csv_module
        from intake_new import BASE_TABLE, ACRONYM, _acronym_rows, norm
        object_fields(payload, {'title', 'proposed', 'confirm'})
        if payload.get('confirm') is not True:
            raise RequestError('保存期刊缩写需要明确确认')
        title = payload.get('title')
        proposed = payload.get('proposed')
        if not isinstance(title, str) or not title.strip() or len(title) > 300 or ('\n' in title) or title.strip().startswith(('=', '+', '-', '@', '\t')) or any((ord(c) < 32 for c in title)):
            raise RequestError('刊名无效（不能为空、含公式注入或换行）')
        if not isinstance(proposed, str) or not ACRONYM.fullmatch(proposed.strip()):
            raise RequestError('缩写仅限 1–12 位字母数字（这是文件名/引用键短码，不是 ISO 4 缩写）')
        title, proposed = (title.strip(), proposed.strip())
        with self.lock, workflow.launch_lock():
            user_table = workflow.REPORTS / 'journal_acronyms.csv'
            if not user_table.is_file():
                user_table.write_text('title,proposed\n', encoding='utf-8')
            existing = {norm(t): p for t, p, _ in _acronym_rows(user_table, 'user')}
            key = norm(title)
            if key in existing and existing[key] != proposed:
                raise RequestError(f'该刊名已映射为 {existing[key]}；如需改为 {proposed}，请直接编辑 {user_table.name}')
            if key in existing:
                return {'title': title, 'proposed': existing[key], 'changed': False, 'message': '该刊名已有相同映射，未重复写入。'}
            base = {norm(t): p for t, p, _ in _acronym_rows(BASE_TABLE, 'base')} if BASE_TABLE.is_file() else {}
            with user_table.open('r', encoding='utf-8-sig', newline='') as stream:
                raw = [row for row in csv_module.reader(stream) if any((cell.strip() for cell in row))]
            if not raw:
                raw = [['title', 'proposed']]
            header = raw[0]
            try:
                title_idx, proposed_idx = (header.index('title'), header.index('proposed'))
            except ValueError:
                raise RequestError('覆盖表缺少 title/proposed 列；请先修复该表再保存') from None
            new_row = [''] * len(header)
            new_row[title_idx] = title
            new_row[proposed_idx] = proposed
            rows = raw + [new_row]
            temporary = user_table.with_name(user_table.name + f'.{os.getpid()}.tmp')
            try:
                with temporary.open('w', encoding='utf-8', newline='') as stream:
                    csv_module.writer(stream, lineterminator='\n').writerows(rows)
                os.replace(temporary, user_table)
            finally:
                temporary.unlink(missing_ok=True)
            overridden = base.get(key)
            return {'title': title, 'proposed': proposed, 'changed': True, 'overrides_base': bool(overridden), 'message': ('已保存（覆盖基础表中的 ' + overridden + '）' if overridden else '已保存') + '；重新预演后生效。新映射仍会经过键/附件授权核验，不会绕过。'}

    def dispatch(self, method, path, payload):
        from model_api import public_config, save_config, save_keys, model_choices, test_connection, profile_defaults, validate_profile, zcode_plan_summary
        self.last_access = time.monotonic()
        if method == 'GET' and path == '/v1/status':
            return self.status()
        if method == 'GET' and path.startswith('/v1/status/'):
            run_id = progress._run_id(path.removeprefix('/v1/status/'))
            return self.status(run_id)
        if method == 'GET' and path == '/v1/history':
            return self.history()
        if method == 'GET' and path.startswith('/v1/diagnosis/'):
            from library_remedy import diagnose
            parts = path.removeprefix('/v1/diagnosis/').split('/')
            if len(parts) != 2 or not re.fullmatch('[A-Z0-9]{8}', parts[1]):
                raise RequestError('诊断路径必须为 /v1/diagnosis/<run_id>/<item_key>')
            progress._run_id(parts[0])
            return diagnose(workflow.RUNS, parts[0], parts[1])
        if method == 'POST' and path == '/v1/diagnosis':
            from library_remedy import diagnose
            object_fields(payload, {'run_id', 'key'})
            progress._run_id(payload.get('run_id'))
            if not isinstance(payload.get('key'), str) or not re.fullmatch('[A-Z0-9]{8}', payload['key']):
                raise RequestError('条目 key 必须为 8 位大写字母/数字')
            return diagnose(workflow.RUNS, payload['run_id'], payload['key'])
        if method == 'POST' and path == '/v1/remedy':
            object_fields(payload, {'run_id', 'key', 'action', 'preview', 'confirm', 'params', 'token'})
            run_id = progress._run_id(payload.get('run_id'))
            key = payload.get('key')
            action = payload.get('action')
            params = payload.get('params', {})
            token = payload.get('token')
            if not isinstance(key, str) or not re.fullmatch('[A-Z0-9]{8}', key):
                raise RequestError('条目 key 必须为 8 位大写字母/数字')
            if not isinstance(action, str) or not re.fullmatch('[a-z_]{1,32}', action):
                raise RequestError('处置动作无效')
            if not isinstance(params, dict):
                raise RequestError('处置参数必须是对象')
            confirm = payload.get('confirm') is True
            if payload.get('preview') is True and confirm:
                raise RequestError('预览与确认不能同时选择')
            if not confirm and payload.get('preview') is not True:
                raise RequestError('处置需要先预览再明确确认')
            if workflow.active_controller():
                raise RequestError('已有同步任务运行；请等待或暂停当前任务后再处置单篇文献')
            from zotero_local import Client
            from library_remedy import binding as remedy_binding, remedy

            def run_remedy():
                return remedy(Client(apply=False), workflow.RUNS, run_id, key, action, params, confirm=confirm, expected_binding=bound_inputs)
            if payload.get('preview') is True:

                def make_preview():
                    from library_remedy import binding as compute_binding
                    before = compute_binding(Client(apply=False), workflow.RUNS, run_id, key, action)
                    result = remedy(Client(apply=False), workflow.RUNS, run_id, key, action, confirm=False)
                    result['binding'] = compute_binding(Client(apply=False), workflow.RUNS, run_id, key, action)
                    if result['binding'] != before:
                        raise RequestError('预览期间输入变化；请重新预览')
                    result['token'] = secrets.token_urlsafe(24)
                    with self.lock:
                        now = time.monotonic()
                        self.remedy_previews = {t: (at, value) for t, (at, value) in self.remedy_previews.items() if now - at < PREVIEW_TTL}
                        if len(self.remedy_previews) >= 32:
                            self.remedy_previews.pop(next(iter(self.remedy_previews)), None)
                        self.remedy_previews[result['token']] = (now, result['binding'])
                    return result
                return self.submit(make_preview)
            if not isinstance(token, str) or not token:
                raise RequestError('确认处置需要先预览取得一次性令牌')
            with self.lock:
                now = time.monotonic()
                stored = self.remedy_previews.get(token)
                if stored is None:
                    raise RequestError('预览令牌不存在或已使用；请重新预览后再确认')
                created, bound_inputs = stored
                if now - created > PREVIEW_TTL:
                    del self.remedy_previews[token]
                    raise RequestError('预览已过期；请重新预览后再确认')
                del self.remedy_previews[token]
            fresh = remedy_binding(Client(apply=False), workflow.RUNS, run_id, key, action)
            if fresh != bound_inputs:
                raise RequestError('预览已过期：文献字节、模型配置或身份在预览后发生变化；请重新预览')
            return self.submit(run_remedy)
        if method == 'POST' and path in ('/v1/pause', '/v1/resume'):
            from library_control import request_pause, launch_resume
            fields = {'run_id', 'confirm'} | ({'finalize_only', 'use_saved_model'} if path == '/v1/resume' else set())
            object_fields(payload, fields)
            run_id = progress._run_id(payload.get('run_id'))
            if payload.get('confirm') is not True:
                raise RequestError('暂停/继续需要明确点击确认')
            if path == '/v1/pause':
                return request_pause(run_id)
            finalize_only = payload.get('finalize_only', False)
            if type(finalize_only) is not bool:
                raise RequestError('仅收尾选项必须为布尔值')
            use_saved_model = payload.get('use_saved_model', False)
            if type(use_saved_model) is not bool:
                raise RequestError('使用已保存模型选项必须为布尔值')
            if use_saved_model and finalize_only:
                raise RequestError('仅收尾恢复不运行模型步骤，无需选择模型')
            return launch_resume(run_id, finalize_only=finalize_only, use_saved_model=use_saved_model)
        if method == 'POST' and path == '/v1/resume/preview':
            from library_control import prepare_recovery, prepare_resume
            object_fields(payload, {'run_id', 'recovery'})
            run_id = progress._run_id(payload.get('run_id'))
            recovery = payload.get('recovery', False)
            if type(recovery) is not bool:
                raise RequestError('遗留恢复选项必须为布尔值')
            from zotero_local import Client

            def resume_preview():
                from model_api import load_config, validate_config
                prepared = (prepare_recovery if recovery else prepare_resume)(Client(apply=False), run_id)
                frozen = prepared['original'].get('model_config')
                saved_summary, saved_ok, saved_error = (None, False, '')
                try:
                    saved = load_config()
                    if saved is not None:
                        saved_summary = validate_config(saved)
                        saved_ok = True
                except Exception as error:
                    saved_error = safe_error(error)
                return {'run_id': run_id, 'steps': prepared['steps'], 'needs_action': prepared.get('needs_action', {}), 'evidence': prepared.get('evidence', {}), 'frozen_model_config': frozen, 'saved_model_config': saved_summary, 'saved_model_ok': saved_ok, 'saved_model_error': saved_error, 'recovery': bool(prepared.get('recovery')) or recovery, 'dry_run': True}
            return self.submit(resume_preview)
        if method == 'POST' and path == '/v1/recovery':
            from library_control import launch_resume
            object_fields(payload, {'run_id', 'confirm', 'use_saved_model'})
            run_id = progress._run_id(payload.get('run_id'))
            if payload.get('confirm') is not True:
                raise RequestError('遗留恢复需要先看预览并明确确认')
            use_saved_model = payload.get('use_saved_model', False)
            if type(use_saved_model) is not bool:
                raise RequestError('使用已保存模型选项必须为布尔值')
            return launch_resume(run_id, recovery=True, use_saved_model=use_saved_model)
        if method == 'POST' and path == '/v1/acronyms':
            return self.save_acronym(payload)
        if method == 'GET' and path == '/v1/config':
            return {**public_config(), 'profileDefaults': profile_defaults(), 'zcodePlans': zcode_plan_summary()}
        if method == 'GET' and path == '/v1/mineru':
            from mineru_credentials import public_status
            return public_status()
        if method == 'GET' and path.startswith('/v1/operations/'):
            return self.get_operation(path.removeprefix('/v1/operations/'))
        if method == 'POST' and path == '/v1/credentials/reveal':
            object_fields(payload, {'provider', 'confirm'})
            if set(payload) != {'provider', 'confirm'} or payload.get('confirm') is not True:
                raise RequestError('显示密钥需要明确确认')
            provider = payload['provider']
            if type(provider) is not str or provider not in ('glm', 'deepseek', 'tongji', 'mineru'):
                raise RequestError('不支持的密钥服务商')
            try:
                from model_api import reveal_credential
                with self.lock:
                    return reveal_credential(provider)
            except Exception:
                pass
            raise RequestError(REVEAL_ERROR)
        if method == 'POST' and path == '/v1/config':
            object_fields(payload, {'config', 'keys'})
            keys = payload.get('keys') or {}
            with self.lock, workflow.launch_lock():
                active = workflow.active_controller()
                if any((not f.done() for oid, f in self.operations.items() if self.operation_kinds.get(oid) != 'catalog')):
                    raise RequestError('预检或连接测试运行中不能改模型配置；结束后再保存')
                if keys:
                    if active or any((not f.done() for f in self.operations.values())):
                        raise RequestError('任务或预检运行中不能更新密钥；结束后再保存')
                    workflow.assert_idle()
                elif not active:
                    workflow.assert_idle()
                save_config(payload.get('config'), keys=keys)
                if keys:
                    self.catalog_generation += 1
            return public_config()
        if method == 'POST' and path in ('/v1/keys', '/v1/mineru'):
            object_fields(payload, {'keys'} if path == '/v1/keys' else {'key'})
            with self.lock, workflow.launch_lock():
                if workflow.active_controller() or any((not f.done() for f in self.operations.values())):
                    raise RequestError('任务或预检运行中不能更新密钥；结束后再保存')
                workflow.assert_idle()
                if path == '/v1/keys':
                    save_keys(payload.get('keys'))
                    self.catalog_generation += 1
                    return public_config()
                from mineru_credentials import save_token, public_status
                save_token(payload.get('key', ''))
                return public_status()
        if method == 'POST' and path == '/v1/preview':
            return self.submit(lambda: self.make_preview(payload))
        if method == 'POST' and path == '/v1/preview/rebind':
            return self.rebind_preview(payload)
        if method == 'POST' and path == '/v1/start':
            return self.start(payload)
        if method == 'POST' and path in ('/v1/models', '/v1/test', '/v1/model/test'):
            fields = {'provider', 'model', 'reasoning_effort', 'effort', 'timeout', 'max_tokens', 'plan', 'base_url', 'protocol', 'models_url', 'thinking', 'extra_body', 'endpoint_confirmed'}
            test_fields = {'role'} if path != '/v1/models' else {'refresh'}
            object_fields(payload, fields | {'profile'} | test_fields)
            role = payload.get('role', 'qc')
            if type(role) is not str or role not in ('qc', 'zh'):
                raise RequestError('测试角色必须为 qc 或 zh')
            wrapped = 'profile' in payload
            if wrapped and set(payload) - ({'profile'} | test_fields):
                raise RequestError('完整 profile 不能与外层模型字段混用')
            selected = copy.deepcopy(payload['profile'] if wrapped else {k: v for k, v in payload.items() if k != 'role'})
            if path == '/v1/models':
                refresh = payload.get('refresh', True)
                if type(refresh) is not bool:
                    raise RequestError('模型列表刷新标志必须为布尔值')
            else:
                refresh = None
            selected.pop('refresh', None)
            object_fields(selected, fields)
            if 'effort' in selected:
                if 'reasoning_effort' in selected:
                    raise RequestError('effort 与 reasoning_effort 不能同时提供')
                selected['reasoning_effort'] = selected.pop('effort')
            if path != '/v1/models' and (not wrapped) and ('model' not in selected) and (set(selected) <= {'provider', 'reasoning_effort'}):
                if selected.get('provider') not in ('glm', 'deepseek', 'tongji'):
                    raise RequestError('不支持的 API 服务商')
                return self.submit(lambda: test_connection(selected['provider'], reasoning_effort=selected.get('reasoning_effort'), role=role))
            selected = validate_profile(selected, for_models=path == '/v1/models')
            if path == '/v1/models':
                return self.submit(lambda: self.fetch_catalog(selected, refresh=refresh), catalog=True, merge_key=self._catalog_key(selected), refresh=refresh)
            return self.submit(lambda: test_connection(profile=selected, role=role))
        raise RequestError('不支持的操作；此接口不能执行任意命令或读写任意文件')

class Handler(BaseHTTPRequestHandler):
    server_version = 'LibrarySync/0.1'

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(data)

    def handle_request(self, method):
        expected_host = f'127.0.0.1:{self.server.server_port}'
        origins = (None, 'null', 'chrome://library-sync')
        if self.client_address[0] != '127.0.0.1' or self.headers.get('Host') != expected_host or self.headers.get('Origin') not in origins or (len(self.headers.get_all('X-Library-Sync-Token', [])) != 1) or (not hmac.compare_digest(self.headers.get('X-Library-Sync-Token', '').encode('utf-8'), self.server.token.encode('ascii'))):
            self.respond(403, {'error': '本机桥接授权无效', 'code': 'AUTH'})
            return
        try:
            if '?' in self.path or '#' in self.path or self.headers.get('Transfer-Encoding'):
                raise RequestError('不接受查询参数或分块请求')
            payload = {}
            if method == 'POST':
                if len(self.headers.get_all('Content-Length', [])) != 1:
                    raise RequestError('请求长度无效')
                length = int(self.headers['Content-Length'])
                if not 0 < length <= MAX_BODY or self.headers.get_content_type() != 'application/json':
                    raise RequestError('只接受有界 JSON 请求')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise RequestError('请求未完整传输')
                payload = parse_json(raw)
                if not isinstance(payload, dict):
                    raise RequestError('请求必须是 JSON 对象')
            result = self.server.service.dispatch(method, self.path, payload)
            self.respond(200, result)
        except Exception as error:
            message = REVEAL_ERROR if self.path == '/v1/credentials/reveal' else safe_error(error)
            code = 'PROGRESS' if isinstance(error, progress.ProgressError) else 'OPERATION'
            if isinstance(error, RequestError):
                code = 'INPUT'
            detail = {'error': message} if self.path == '/v1/credentials/reveal' else {'error': message, 'code': code, 'retry_automatically': False}
            self.respond(400, detail)

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

def write_private_manifest(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'darwin':
        if path.parent.is_symlink() or path.parent.stat().st_uid != os.getuid():
            raise RuntimeError('桥接凭据目录的所有权无效')
        os.chmod(path.parent, 448)
        acl = subprocess.run(['/bin/chmod', '-N', str(path.parent)], capture_output=True, timeout=5)
        if acl.returncode:
            raise RuntimeError('不能保护桥接凭据目录的 macOS 访问权限')
        from zotero_local import _atomic_bytes
        _atomic_bytes(path, json.dumps(value).encode('utf-8'))
        os.chmod(path, 384)
        return
    identity = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'], capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    sids = re.findall(b'S-1-5(?:-\\d+)+', identity.stdout)
    if identity.returncode or len(sids) != 1:
        raise RuntimeError('不能确认当前 Windows 用户，未发布桥接凭据')
    fd, temporary = tempfile.mkstemp(prefix='.plugin-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            acl = subprocess.run(['icacls.exe', temporary, '/inheritance:r', '/grant:r', '*' + sids[0].decode('ascii') + ':(F)'], capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            if acl.returncode:
                raise RuntimeError('不能保护桥接凭据文件权限，未启动桥接')
            output.write(json.dumps(value).encode('utf-8'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def serve():
    if os.name != 'nt' and sys.platform != 'darwin':
        raise RuntimeError('此版本支持 Windows 和 macOS')
    lock = FileLock(workflow.RUNS / 'plugin_bridge.lock')
    try:
        lock.acquire(timeout=0)
    except TimeoutError:
        return 0
    try:
        service = Service()
        from report_retention import expire_reports
        next_cleanup = 0.0
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            server.daemon_threads = True
            server.service, server.token = (service, secrets.token_urlsafe(32))
            server.timeout = 1
            write_private_manifest(MANIFEST, {'schema': 1, 'host': '127.0.0.1', 'port': server.server_port, 'token': server.token, 'pid': os.getpid()})
            while time.monotonic() - service.last_access < 1800 or any((not f.done() for f in list(service.operations.values()))):
                server.handle_request()
                if time.monotonic() >= next_cleanup:
                    expire_reports(workflow.REPORTS, workflow.RUNS, apply=True)
                    next_cleanup = time.monotonic() + 24 * 60 * 60
        service.pool.shutdown(wait=False)
        service.catalog_pool.shutdown(wait=False)
    finally:
        lock.release()
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--serve', action='store_true')
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument('--apply', action='store_true')
    modes.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=0, help='reserved; never triggers a paper sync')
    args = ap.parse_args(argv)
    if args.serve and args.apply:
        return serve()
    print('dry-run: 本机桥接尚未启动；--serve --apply 仅启动面板桥接，不同步论文。')
    return 0
if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        if sys.stderr is not None:
            print(safe_error(error), file=sys.stderr)
        raise SystemExit(2)
