# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import copy
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
import requests
GLM_CHAT_DOCUMENT = 'https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8.md'
GLM_FLASH_DOCUMENT = 'https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash.md'
GLM_CODING_DOCUMENT = 'https://docs.bigmodel.cn/cn/coding-plan/overview.md'
OFFICIAL_DOCUMENTS = frozenset({GLM_CHAT_DOCUMENT, GLM_FLASH_DOCUMENT, GLM_CODING_DOCUMENT})
MAX_DOCUMENT_BYTES = 192 * 1024
DOCUMENT_TIMEOUT = (3, 5)
MAX_FETCH_SECONDS = 8
MAX_MODELS = 512
BUNDLED_VERIFIED_AT = '2026-10-26'
GLM_MODEL_CANDIDATES = ('glm-5.3', 'glm-5.3-flash', 'glm-5.3-flashx', 'glm-5.3-flashx-legal', 'glm-5.2', 'glm-5.1', 'glm-5-turbo', 'glm-5', 'glm-4.7', 'glm-4.7-flash', 'glm-4.7-flashx', 'glm-4.6', 'glm-4.5-air', 'glm-4.5-airx', 'glm-4.5-flash', 'glm-4-flash-250414', 'glm-4-flashx-250414')
GLM_CODING_CANDIDATES = ('glm-5.3', 'glm-5.3-flash')
_GLM_ID = re.compile('glm-[a-z0-9][a-z0-9_.-]{0,250}\\Z')
_EFFORT_ORDER = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')

class CatalogError(RuntimeError):
    pass

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def endpoint_kind(provider, base_url):
    try:
        parts = urlsplit(base_url)
        if parts.scheme != 'https' or parts.username or parts.password or parts.query or parts.fragment or (parts.port not in (None, 443)):
            return 'custom'
        host, path = (parts.hostname, parts.path.rstrip('/'))
        if provider == 'glm' and host == 'open.bigmodel.cn':
            if path == '/api/paas/v4':
                return 'standard'
            if path == '/api/coding/paas/v4':
                return 'coding_plan'
        if provider == 'deepseek' and host == 'api.deepseek.com' and (path in ('', '/v1')):
            return 'standard'
        if provider == 'tongji' and host == 'agi.tongji.edu.cn' and (path == '/v1'):
            return 'tongji'
    except (TypeError, ValueError):
        pass
    return 'custom'

def highest_effort(levels):
    if not levels or any((level not in _EFFORT_ORDER for level in levels)):
        return None
    return max(levels, key=_EFFORT_ORDER.index)

def known_details(provider, model, *, kind='standard'):
    if kind not in ('standard', 'coding_plan'):
        return {}
    if kind == 'coding_plan' and (provider != 'glm' or model not in GLM_CODING_CANDIDATES):
        return {}
    if provider == 'glm' and model in GLM_MODEL_CANDIDATES[:4]:
        return {'context_window': 1048576, 'max_output_tokens': 131072, 'effort_levels': ['low', 'high', 'max'], 'default_effort': 'max', 'highest_effort': 'max'}
    if provider == 'glm' and model == 'glm-5.2':
        return {'max_output_tokens': 131072, 'effort_levels': ['max', 'xhigh', 'high', 'medium', 'low', 'minimal', 'none'], 'default_effort': 'max', 'highest_effort': 'max'}
    if provider == 'deepseek' and model in ('deepseek-flash', 'deepseek-v4-pro'):
        return {'context_window': 1048576, 'max_output_tokens': 393216, 'effort_levels': ['low', 'high', 'max'], 'default_effort': 'high', 'highest_effort': 'max'}
    return {}

def enrich_details(provider, model, details, *, kind='standard'):
    result = known_details(provider, model, kind=kind)
    result.update(copy.deepcopy(details))
    if 'effort_levels' in details:
        result.pop('highest_effort', None)
        highest = highest_effort(details['effort_levels'])
        if highest is not None:
            result['highest_effort'] = highest
        if result.get('default_effort') not in details['effort_levels']:
            result.pop('default_effort', None)
    return result

def _document_text(value):
    if isinstance(value, bytes):
        if len(value) > MAX_DOCUMENT_BYTES:
            raise CatalogError('document_too_large')
        try:
            value = value.decode('utf-8-sig')
        except UnicodeError:
            raise CatalogError('invalid_document_encoding') from None
    if type(value) is not str or len(value.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise CatalogError('invalid_document')
    if '\x00' in value:
        raise CatalogError('invalid_document')
    return value

def fetch_official_markdown(url):
    if url not in OFFICIAL_DOCUMENTS:
        raise CatalogError('document_url_not_allowed')
    started = time.monotonic()
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.auth = None
            session.cookies.clear()
            with session.get(url, timeout=DOCUMENT_TIMEOUT, allow_redirects=False, stream=True, verify=True, headers={'Accept': 'text/markdown'}) as response:
                if response.status_code != 200:
                    raise CatalogError('document_http_error')
                declared = response.headers.get('Content-Length')
                if declared is not None and (not declared.isdigit() or int(declared) > MAX_DOCUMENT_BYTES):
                    raise CatalogError('document_too_large')
                chunks, size = ([], 0)
                for chunk in response.iter_content(chunk_size=8192):
                    if time.monotonic() - started > MAX_FETCH_SECONDS:
                        raise CatalogError('document_timeout')
                    size += len(chunk)
                    if size > MAX_DOCUMENT_BYTES:
                        raise CatalogError('document_too_large')
                    chunks.append(chunk)
                return _document_text(b''.join(chunks))
    except CatalogError:
        raise
    except Exception:
        raise CatalogError('document_fetch_failed') from None

def _ids(values):
    result = []
    for value in values:
        if not _GLM_ID.fullmatch(value):
            raise CatalogError('invalid_document_model')
        if value not in result:
            result.append(value)
        if len(result) > MAX_MODELS:
            raise CatalogError('too_many_document_models')
    if not result:
        raise CatalogError('document_models_missing')
    return result

def _yaml_child(lines, key):
    pattern = re.compile('^( *)' + re.escape(key) + ':\\s*(?:#.*)?$')
    for index, line in enumerate(lines):
        match = pattern.fullmatch(line)
        if not match:
            continue
        indent = len(match[1])
        end = index + 1
        while end < len(lines):
            item = lines[end]
            if item.strip() and len(item) - len(item.lstrip(' ')) <= indent:
                break
            end += 1
        return lines[index + 1:end]
    raise CatalogError('document_schema_changed')

def parse_chat_models(document):
    text = _document_text(document)
    blocks = re.findall('(?ms)^(`{3,})ya?ml[^\\n]*\\n(.*?)^\\1\\s*$', text)
    for _, block in blocks:
        if not re.search('(?m)^openapi:\\s*3\\.', block):
            continue
        lines = block.splitlines()
        for key in ('components', 'schemas', 'ChatCompletionTextRequest', 'properties', 'model', 'enum'):
            lines = _yaml_child(lines, key)
        values = []
        for line in lines:
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = re.fullmatch('\\s*-\\s*[\'\\"]?(glm-[a-z0-9_.-]+)[\'\\"]?\\s*(?:#.*)?', line)
            if not match:
                raise CatalogError('document_schema_changed')
            values.append(match[1])
        return _ids(values)
    raise CatalogError('document_schema_changed')

def parse_flash_models(document):
    text = _document_text(document)
    for line in text.splitlines():
        if re.match('^\\s*(?:\\*\\*)?Model Code(?:\\*\\*)?\\s*[:：]', line):
            return _ids(re.findall('`(glm-[a-z0-9_.-]+)`', line))
    raise CatalogError('document_model_codes_missing')

def parse_coding_models(document):
    text = _document_text(document)
    match = re.search('(?m)^##\\s+可用模型\\s*$', text)
    if not match:
        raise CatalogError('document_available_models_missing')
    section = re.split('(?m)^#{1,2}\\s+', text[match.end():], maxsplit=1)[0]
    values = []
    for bullet in re.split('(?m)^\\s*[*+-]\\s+', section)[1:]:
        if re.search('历史|旧模型|切换|映射|迁移|未开放|不支持', bullet):
            continue
        if '支持' not in bullet:
            continue
        values.extend((value.lower() for value in re.findall('(?i)\\bGLM-[a-z0-9]+(?:[._-][a-z0-9]+)*', bullet)))
    return _ids(values)

class OfficialModelCatalog:

    def __init__(self, fetcher=None, now=None):
        self.fetcher = fetcher
        self.now = utc_now if now is None else now
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, kind='standard', *, refresh=True):
        if kind not in ('standard', 'coding_plan'):
            raise CatalogError('unsupported_catalog_endpoint')
        reference = GLM_CODING_DOCUMENT if kind == 'coding_plan' else GLM_CHAT_DOCUMENT
        if refresh:
            try:
                fetch = fetch_official_markdown if self.fetcher is None else self.fetcher
                if kind == 'coding_plan':
                    models = parse_coding_models(fetch(GLM_CODING_DOCUMENT))
                else:
                    models = parse_chat_models(fetch(GLM_CHAT_DOCUMENT))
                    flash = parse_flash_models(fetch(GLM_FLASH_DOCUMENT))
                    models = _ids(models[:1] + flash + models[1:])
                snapshot = {'models': models, 'fetched_at': self.now()}
                with self._lock:
                    self._cache[kind] = copy.deepcopy(snapshot)
                return self._result(kind, reference, snapshot, 'official_docs')
            except (CatalogError, requests.RequestException, TimeoutError, OSError):
                pass
        with self._lock:
            cached = copy.deepcopy(self._cache.get(kind))
        if cached is not None:
            return self._result(kind, reference, cached, 'official_cache')
        bundled = GLM_CODING_CANDIDATES if kind == 'coding_plan' else GLM_MODEL_CANDIDATES
        return self._result(kind, reference, {'models': list(bundled), 'fetched_at': None}, 'bundled_catalog')

    @staticmethod
    def _result(kind, reference, snapshot, source):
        notices = {'official_docs': '在线读取官方公开文档候选，非账户可用清单；实际调用权限需测试连接确认。', 'official_cache': '官方文档缓存（非实时），非账户可用清单；实际调用权限需测试连接确认。', 'bundled_catalog': f'内置官方文档候选（{BUNDLED_VERIFIED_AT} 核验，非实时），非账户可用清单；使用前需测试连接确认权限。'}
        notice = notices[source] + f'能力字段来自 {BUNDLED_VERIFIED_AT} 已核实资料快照（非实时）。'
        notice += 'GLM-5.3 系列 effort 在 thinking 开启时生效，最高 max；thinking 仅支持 enabled。'
        if kind == 'coding_plan':
            notice += '仅列 Coding Plan 可用模型；不将历史映射视为独立模型。'
        details = {model: known_details('glm', model, kind=kind) for model in snapshot['models']}
        return {'provider': 'glm', **copy.deepcopy(snapshot), 'source': source, 'notice': notice, 'endpoint_kind': kind, 'reference': reference, 'details': {model: value for model, value in details.items() if value}}
DEFAULT_CATALOG = OfficialModelCatalog()
