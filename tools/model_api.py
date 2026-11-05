# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import copy
import ipaddress
import json as jsonlib
import math
import re
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit
import requests
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
import model_catalog
from glm_concurrency import FileLock, glm_slot
from zotero_local import _atomic_bytes, _dpapi
from library_paths import REPO, SECRETS
DEFAULT_CONFIG = SECRETS / 'model_api.dpapi'
ENDPOINTS = MappingProxyType({'glm': 'https://open.bigmodel.cn/api/paas/v4/chat/completions', 'deepseek': 'https://api.deepseek.com/chat/completions', 'tongji': 'https://agi.tongji.edu.cn/v1/chat/completions'})
MODEL_ENDPOINTS = MappingProxyType({'deepseek': 'https://api.deepseek.com/models', 'tongji': 'https://agi.tongji.edu.cn/v1/models'})
TONGJI_HOSTS = frozenset({'agi.tongji.edu.cn', 'llmapi.tongji.edu.cn'})
TOKEN_LIMITS = MappingProxyType({'glm': 131072, 'deepseek': 393216, 'tongji': 1048576})
BASE_URLS = MappingProxyType({p: url.removesuffix('/chat/completions') for p, url in ENDPOINTS.items()})
PROTOCOL = 'openai_chat_completions'
GLM_MODEL_CANDIDATES = model_catalog.GLM_MODEL_CANDIDATES
GLM_MODEL_REFERENCE = model_catalog.GLM_CHAT_DOCUMENT
GLM_FLASH_REFERENCE = model_catalog.GLM_FLASH_DOCUMENT
GLM_THINKING_REFERENCE = 'https://docs.bigmodel.cn/cn/guide/capabilities/thinking'
EFFORTS = frozenset({'default', 'none', 'low', 'high', 'max'})
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_MODELS = 512
MAX_EFFORT_LEVELS = 16
MAX_EXTRA_BYTES = 16384
_MODEL_ID = re.compile('[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}\\Z')
_EFFORT = re.compile('[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\\Z')
_V2_FIELDS = frozenset({'base_url', 'protocol', 'models_url', 'thinking', 'extra_body', 'endpoint_confirmed'})
_PROFILE_FIELDS = frozenset({'provider', 'model', 'reasoning_effort', 'timeout', 'max_tokens'}) | _V2_FIELDS
_EXTRA_BLOCKED = frozenset({'model', 'messages', 'stream', 'streamoptions', 'maxtokens', 'maxcompletiontokens', 'reasoningeffort', 'thinking', 'headers', 'header', 'extraheaders', 'defaultheaders', 'authorization', 'authentication', 'auth', 'apikey', 'xapikey', 'key', 'keys', 'token', 'accesstoken', 'bearer', 'cookie', 'cookies', 'password', 'secret', 'credentials', 'credential', 'baseurl', 'modelsurl', 'url', 'endpoint', 'protocol', 'provider', 'endpointconfirmed', 'proxy', 'proxies', 'verify', 'cert', 'timeout', 'tools', 'toolchoice', 'toolcalls', 'toolstream', 'functions', 'functioncall', 'paralleltoolcalls', 'extrabody', 'extraquery', 'query', 'params'})
_TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')

class ModelAPIError(RuntimeError):
    stop_workflow = True

    def __init__(self, code: str, status: int | None=None, finish_reason=None):
        self.code, self.status = (code, status)
        self.finish_reason = finish_reason
        message = code if status is None else f'{code} (HTTP {status})'
        if finish_reason is not None:
            message += f' (finish_reason={finish_reason})'
        super().__init__(message)

class ConfigError(ModelAPIError):
    pass

class UnsupportedOperation(ModelAPIError):
    pass

class ImmutableDict(dict):
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        value = dict.__new__(cls)
        dict.__init__(value, *args, **kwargs)
        return value

    def __init__(self, *args, **kwargs):
        pass

    def _immutable(self, *args, **kwargs):
        raise TypeError('immutable dictionary')
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

def _provider(value):
    if not isinstance(value, str) or value not in ENDPOINTS:
        raise ConfigError('invalid_provider')
    return value

def _model(value):
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value) or '://' in value:
        raise ConfigError('invalid_model_id')
    return value

def _integer(value, maximum, code):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ConfigError(code)
    return value

def _public_ip(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and (not (address.is_multicast or address.is_reserved or address.is_unspecified))

def _endpoint_url(value, *, base=False):
    try:
        if type(value) is not str or not 1 <= len(value) <= 2048 or any((not 33 <= ord(c) <= 126 for c in value)) or any((c in value for c in ('?', '#', '\\', '%'))):
            raise ValueError
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if parts.scheme != 'https' or not host or parts.username is not None or (parts.password is not None) or parts.query or parts.fragment or (port is not None and (not 1 <= port <= 65535)):
            raise ValueError
        host = host.lower().rstrip('.')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if host == 'localhost' or host.endswith(('.localhost', '.local', '.localdomain', '.internal', '.lan')) or len(host) > 253 or ('.' not in host) or (not re.fullmatch('[a-z][a-z0-9-]*', host.rsplit('.', 1)[-1])) or any((not re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.'))):
                raise ValueError
        else:
            if not _public_ip(str(address)):
                raise ValueError
            host = f'[{address.compressed}]' if address.version == 6 else str(address)
        path = parts.path.rstrip('/') if base else parts.path
        if not re.fullmatch("[A-Za-z0-9/._~!$&'()*+,;=:@-]*", path) or any((segment in ('.', '..') for segment in path.split('/'))) or (base and path.lower().endswith('/chat/completions')):
            raise ValueError
        authority = host if port in (None, 443) else f'{host}:{port}'
        return urlunsplit(('https', authority, path, '', ''))
    except Exception:
        pass
    raise ConfigError('invalid_endpoint_url')

def _extra_body(value):
    if type(value) is not dict:
        raise ConfigError('invalid_extra_body')
    count = 0

    def visit(item, depth=0):
        nonlocal count
        count += 1
        if depth > 5 or count > 256:
            raise ValueError
        if type(item) is dict:
            if len(item) > 64:
                raise ValueError
            for name, child in item.items():
                if type(name) is not str or not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', name) or re.sub('[_.-]', '', name).lower() in _EXTRA_BLOCKED:
                    raise ValueError
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            if len(item) > 4096:
                raise ValueError
        elif type(item) is int:
            if abs(item) > 2 ** 53 - 1:
                raise ValueError
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError
        elif item is not None and type(item) is not bool:
            raise ValueError
    try:
        visit(value)
        encoded = jsonlib.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        if len(encoded.encode('utf-8')) <= MAX_EXTRA_BYTES:
            return jsonlib.loads(encoded)
    except Exception:
        pass
    raise ConfigError('invalid_extra_body')

def _profile(value, *, for_models=False):
    if not isinstance(value, Mapping) or set(value) - _PROFILE_FIELDS:
        raise ConfigError('invalid_profile_fields')
    provider = _provider(value.get('provider'))
    model = value.get('model', '')
    model = '' if for_models and type(model) is str and (not model.strip()) else _model(model)
    effort = value.get('reasoning_effort', 'default')
    if type(effort) is not str or not _EFFORT.fullmatch(effort):
        raise ConfigError('invalid_reasoning_effort')
    normalized = {'provider': provider, 'model': model, 'reasoning_effort': effort, 'timeout': _integer(value.get('timeout', 1500), 1500, 'invalid_timeout'), 'max_tokens': _integer(value.get('max_tokens', 32768), TOKEN_LIMITS[provider], 'invalid_max_tokens')}
    if not set(value) & _V2_FIELDS:
        return normalized
    protocol = value.get('protocol', PROTOCOL)
    if type(protocol) is not str or protocol != PROTOCOL:
        raise UnsupportedOperation('unsupported_protocol')
    thinking = value.get('thinking', 'default')
    if type(thinking) is not str or thinking not in ('default', 'enabled', 'disabled'):
        raise ConfigError('invalid_thinking')
    if provider == 'tongji' and thinking != 'default':
        raise ConfigError('tongji_use_reasoning_effort')
    confirmed = value.get('endpoint_confirmed', False)
    if type(confirmed) is not bool:
        raise ConfigError('invalid_endpoint_confirmation')
    base_url = _endpoint_url(value.get('base_url', BASE_URLS[provider]), base=True)
    models_url = value.get('models_url', MODEL_ENDPOINTS.get(provider, ''))
    if models_url != '':
        models_url = _endpoint_url(models_url)
    public_coding_docs = for_models and provider == 'glm' and (models_url == '') and (base_url == 'https://open.bigmodel.cn/api/coding/paas/v4')
    if not confirmed and (not public_coding_docs) and (base_url != BASE_URLS[provider] or models_url not in ('', MODEL_ENDPOINTS.get(provider, ''))):
        raise ConfigError('endpoint_confirmation_required')
    extra_body = _extra_body(value.get('extra_body', {}))
    if provider == 'tongji':

        def check_thinking_fields(item):
            if isinstance(item, dict):
                for name, child in item.items():
                    if re.sub('[_.-]', '', name).lower() in ('enablethinking', 'chattemplatekwargs'):
                        raise ConfigError('tongji_use_reasoning_effort')
                    check_thinking_fields(child)
            elif isinstance(item, list):
                for child in item:
                    check_thinking_fields(child)
        check_thinking_fields(extra_body)
    normalized.update(base_url=base_url, protocol=protocol, models_url=models_url, thinking=thinking, extra_body=extra_body, endpoint_confirmed=confirmed)
    return normalized

def validate_profile(profile, *, for_models=False):
    return _profile(profile, for_models=for_models)

def profile_defaults():
    return {provider: _profile({'provider': provider, 'model': '', 'protocol': PROTOCOL, 'max_tokens': 32768 if provider == 'tongji' else TOKEN_LIMITS[provider], 'reasoning_effort': 'default' if provider == 'tongji' else 'max', 'thinking': 'default' if provider == 'tongji' else 'enabled'}, for_models=True) for provider in ENDPOINTS}

def highest_parameters(profile, details=None):
    selected = _profile(profile, for_models=True)
    model = selected['model']
    kind = _catalog_endpoint_kind(selected['provider'], selected)
    if kind == 'coding_plan' and model not in model_catalog.GLM_CODING_CANDIDATES:
        return {}
    metadata = details if isinstance(details, Mapping) else {}
    if metadata.get('endpoint_kind', kind) != kind or (kind == 'custom' and metadata.get('source') in ('official_docs', 'official_cache', 'bundled_catalog')):
        return {}
    reference = metadata.get('reference')
    if metadata.get('source') == 'provider_api' and reference != _profile_urls(selected['provider'], selected)[1]:
        return {}
    if kind == 'custom' and reference in MODEL_ENDPOINTS.values():
        return {}
    if isinstance(metadata.get('details'), Mapping):
        metadata = metadata['details']
    if model in metadata and isinstance(metadata[model], Mapping):
        metadata = metadata[model]
    kept = _model_details({'max_output_tokens': metadata.get('max_output_tokens'), 'effort': {'supported_levels': metadata.get('effort_levels'), 'default_level': metadata.get('default_effort')}})
    capabilities = model_catalog.enrich_details(selected['provider'], model, kept, kind=kind)
    result = {}
    if 'max_output_tokens' in capabilities:
        result['max_tokens'] = capabilities['max_output_tokens']
    highest = capabilities.get('highest_effort')
    if highest is not None:
        result['reasoning_effort'] = highest
        if selected['provider'] != 'tongji':
            result['thinking'] = 'disabled' if highest == 'none' else 'enabled'
    return result

def _profile_urls(provider, profile=None):
    if profile is None or 'protocol' not in profile:
        return (ENDPOINTS[provider], MODEL_ENDPOINTS.get(provider, ''))
    if profile['provider'] != provider:
        raise ConfigError('profile_provider_mismatch')
    return (profile['base_url'] + '/chat/completions', profile['models_url'])

def _catalog_endpoint_kind(provider, profile=None):
    base_url = profile.get('base_url', BASE_URLS[provider]) if profile else BASE_URLS[provider]
    kind = model_catalog.endpoint_kind(provider, base_url)
    models_url = _profile_urls(provider, profile)[1]
    if models_url and models_url != MODEL_ENDPOINTS.get(provider, ''):
        return 'custom'
    return kind

def _key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any((not 33 <= ord(char) <= 126 for char in value)):
        raise ConfigError('invalid_api_key')
    return value

def _keys(value, *, allow_empty=False):
    if not isinstance(value, Mapping) or set(value) - set(ENDPOINTS):
        raise ConfigError('invalid_key_fields')
    return {p: _key(k) for p, k in value.items() if not (allow_empty and (k is None or k == ''))}

def _store(key_store=None, path=None):
    if key_store is not None and path is not None:
        raise ConfigError('ambiguous_config_store')
    return key_store if key_store is not None else DPAPIConfigStore(path)

def _get_key(store, provider):
    try:
        value = store.get(provider) if isinstance(store, Mapping) else store.get_key(provider)
        if value is None or value == '':
            raise ConfigError('credential_missing')
        return _key(value)
    except ConfigError:
        raise
    except Exception:
        raise ConfigError('credential_unavailable') from None

def validate_config(config, require_credentials=False, *, key_store=None, path=None, required_roles=('qc', 'zh')):
    if not isinstance(config, Mapping) or set(config) != {'schema', 'qc', 'zh'}:
        raise ConfigError('invalid_config_fields')
    if type(config['schema']) is not int or config['schema'] != 1:
        raise ConfigError('unsupported_config_schema')
    normalized = {'schema': 1, 'qc': _profile(config['qc']), 'zh': _profile(config['zh'])}
    if not isinstance(required_roles, (tuple, list)) or not required_roles or any((role not in ('qc', 'zh') for role in required_roles)):
        raise ConfigError('invalid_required_roles')
    if require_credentials:
        store = _store(key_store, path)
        for provider in {normalized[role]['provider'] for role in required_roles}:
            _get_key(store, provider)
    return normalized

class DPAPIConfigStore:

    def __init__(self, path=None, *, crypto=None, atomic_write=None):
        self.path = Path(DEFAULT_CONFIG if path is None else path)
        self._crypto = _dpapi if crypto is None else crypto
        self._atomic_write = _atomic_bytes if atomic_write is None else atomic_write

    def load(self):
        try:
            ciphertext = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise ConfigError('config_read_failed') from None
        try:
            if len(ciphertext) > 1024 * 1024:
                raise ValueError
            document = jsonlib.loads(self._crypto(ciphertext, decrypt=True))
            if not isinstance(document, dict) or set(document) != {'schema', 'config', 'keys'} or type(document['schema']) is not int or (document['schema'] != 1):
                raise ValueError
            return {'schema': 1, 'config': None if document['config'] is None else validate_config(document['config']), 'keys': _keys(document['keys'])}
        except Exception:
            raise ConfigError('config_unprotect_failed') from None

    def get_key(self, provider):
        document = self.load()
        return document['keys'].get(_provider(provider)) if document is not None else None

    def save(self, config, keys=None):
        normalized = validate_config(config)
        return self._save(normalized, keys, replace_config=True)

    def save_keys(self, keys):
        return self._save(None, keys, replace_config=False)

    def _save(self, normalized, keys, *, replace_config):
        updates = {} if keys is None else _keys(keys, allow_empty=True)
        lock = FileLock(self.path.with_name(self.path.name + '.lock'))
        try:
            lock.acquire(timeout=30)
            document = self.load()
            if not replace_config:
                normalized = None if document is None else document['config']
            merged = {} if document is None else document['keys']
            merged.update(updates)
            plaintext = jsonlib.dumps({'schema': 1, 'config': normalized, 'keys': merged}, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
            ciphertext = self._crypto(plaintext)
            self._atomic_write(self.path, ciphertext)
        except ConfigError:
            raise
        except Exception:
            raise ConfigError('config_save_failed') from None
        finally:
            lock.release()
        return normalized

def load_config(*, path=None, key_store=None):
    document = _store(key_store, path).load()
    return None if document is None or document['config'] is None else validate_config(document['config'])

def public_config(*, path=None, key_store=None):
    document = _store(key_store, path).load()
    config = None if document is None or document['config'] is None else validate_config(document['config'])
    credentials = {p: bool(document and document['keys'].get(p)) for p in ENDPOINTS}
    configured = config is not None and all((credentials[config[r]['provider']] for r in ('qc', 'zh')))
    return {'configured': configured, 'config': config, 'credentials': credentials}

def save_config(config, keys=None, *, path=None, key_store=None):
    normalized = validate_config(config)
    return _store(key_store, path).save(normalized, keys)

def save_keys(keys, *, path=None, key_store=None):
    return _store(key_store, path).save_keys(keys)

def reveal_credential(provider, *, key_store=None, path=None):
    if type(provider) is not str or provider not in (*ENDPOINTS, 'mineru'):
        raise ConfigError('invalid_provider')
    failure = 'credential_unavailable'
    try:
        if provider == 'mineru':
            if key_store is not None:
                raise ValueError
            from mineru_credentials import _select
            value, source = _select(path, None, None, None)
        else:
            store = _store(key_store, path)
            value = store.get(provider) if isinstance(store, Mapping) else store.get_key(provider)
            source = 'encrypted'
        if value is None or value == '':
            failure = 'credential_missing'
        else:
            if type(source) is not str or source not in ('encrypted', 'legacy_file', 'environment'):
                raise ValueError
            return {'provider': provider, 'key': _key(value), 'source': source}
    except Exception:
        pass
    raise ConfigError(failure)

@dataclass(frozen=True)
class TransportResult:
    status: int
    data: object = field(default=None, repr=False)

class _PublicHTTPSConnection(HTTPSConnection):

    def _address_allowed(self, value):
        return _public_ip(value)

    def _new_conn(self):
        try:
            answers = socket.getaddrinfo(self._dns_host, self.port, type=socket.SOCK_STREAM)
        except Exception:
            raise ModelAPIError('endpoint_resolution_failed') from None
        try:
            if not answers or len(answers) > 64 or any((family not in (socket.AF_INET, socket.AF_INET6) or not self._address_allowed(address[0]) for family, _, _, _, address in answers)):
                raise ValueError
        except Exception:
            raise ModelAPIError('endpoint_not_allowed') from None
        timed_out = False
        for family, socktype, proto, _, address in answers:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self.timeout)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(address)
                return sock
            except OSError as error:
                timed_out = isinstance(error, TimeoutError)
                if sock is not None:
                    sock.close()
        raise ModelAPIError('request_timeout' if timed_out else 'transport_error')

class _PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection

class _TongjiHTTPSConnection(_PublicHTTPSConnection):

    def _address_allowed(self, value):
        if _public_ip(value):
            return True
        if self._dns_host not in TONGJI_HOSTS or self.port != 443:
            return False
        address = ipaddress.ip_address(value)
        return isinstance(address, ipaddress.IPv4Address) and any((address in ipaddress.ip_network(network) for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))

class _TongjiHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _TongjiHTTPSConnection

class _PublicHTTPSAdapter(requests.adapters.HTTPAdapter):
    pool_class = _PublicHTTPSPool

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {**self.poolmanager.pool_classes_by_scheme, 'https': self.pool_class}

class _TongjiHTTPSAdapter(_PublicHTTPSAdapter):
    pool_class = _TongjiHTTPSPool

def _tongji_campus_url(provider, url):
    parts = urlsplit(url)
    return provider == 'tongji' and parts.scheme == 'https' and (parts.hostname in TONGJI_HOSTS) and (parts.port in (None, 443)) and (parts.path in ('/v1/models', '/v1/chat/completions')) and (parts.username is None) and (parts.password is None) and (not parts.query) and (not parts.fragment)

class RequestsTransport:

    def __init__(self, *, profile=None):
        self._provider = None if profile is None else profile.get('provider')
        if profile is None:
            self._allowed = {('POST', u) for u in ENDPOINTS.values()} | {('GET', u) for u in MODEL_ENDPOINTS.values()}
        else:
            profile = _profile(profile, for_models=True)
            if not profile.get('endpoint_confirmed', False) and profile.get('base_url', BASE_URLS[profile['provider']]) != BASE_URLS[profile['provider']]:
                raise ConfigError('endpoint_confirmation_required')
            chat_url, models_url = _profile_urls(profile['provider'], profile)
            self._allowed = {('POST', chat_url)} | ({('GET', models_url)} if models_url else set())

    def request(self, method, url, *, headers, json, timeout):
        if (method, url) not in self._allowed:
            raise ModelAPIError('endpoint_not_allowed')
        started = time.monotonic()
        with requests.Session() as session:
            session.trust_env = False
            session.verify = True
            adapter = _TongjiHTTPSAdapter if _tongji_campus_url(self._provider, url) else _PublicHTTPSAdapter
            session.mount('https://', adapter(max_retries=0))
            with session.request(method, url, headers=headers, json=json, stream=True, timeout=(min(30, timeout), timeout), verify=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    return TransportResult(response.status_code)
                chunks, size = ([], 0)
                for chunk in response.iter_content(chunk_size=8192):
                    if time.monotonic() - started > timeout:
                        raise ModelAPIError('request_timeout')
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ModelAPIError('response_too_large')
                    chunks.append(chunk)
                try:
                    data = jsonlib.loads(b''.join(chunks))
                except (ValueError, UnicodeError):
                    raise ModelAPIError('invalid_response_json') from None
                return TransportResult(200, data)

def _reject_credential_metadata(data, key, method):

    def check(value):
        if isinstance(value, str) and key in value:
            raise ModelAPIError('credential_in_response_metadata')
    check(data.get('model'))
    entries = data.get('data') if method == 'GET' else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            check(entry.get('id'))
            check(entry.get('name'))
            effort = entry.get('effort')
            if isinstance(effort, dict):
                check(effort.get('default_level'))
                levels = effort.get('supported_levels')
                if isinstance(levels, list):
                    for level in levels:
                        check(level)

def _request(provider, method, payload, *, key_store, transport, timeout, slot_root=None, sleep=time.sleep, profile=None):
    if profile is not None:
        profile = _profile(profile, for_models=method == 'GET')
        if profile['provider'] != provider:
            raise ConfigError('profile_provider_mismatch')
    chat_url, models_url = _profile_urls(provider, profile)
    url = chat_url if method == 'POST' else models_url
    if not url:
        raise UnsupportedOperation('model_listing_not_supported')
    key = _get_key(key_store, provider)
    if key in url:
        raise ConfigError('credential_in_endpoint')
    transport = RequestsTransport(profile=profile or {'provider': provider, 'model': ''}) if transport is None else transport
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with glm_slot(root=slot_root, limit=2, timeout=timeout):
                result = transport.request(method, url, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, json=payload, timeout=timeout)
        except ModelAPIError as exc:
            code = exc.code if type(transport) is RequestsTransport and exc.code in {'endpoint_not_allowed', 'request_timeout', 'response_too_large', 'invalid_response_json', 'endpoint_resolution_failed', 'transport_error'} else 'transport_error'
            raise ModelAPIError(code) from None
        except (TimeoutError, requests.exceptions.Timeout):
            raise ModelAPIError('request_timeout') from None
        except requests.exceptions.SSLError:
            raise ModelAPIError('tls_error') from None
        except Exception:
            raise ModelAPIError('transport_error') from None
        if not isinstance(result, TransportResult) or type(result.status) is not int:
            raise ModelAPIError('invalid_transport_result')
        status = result.status
        if not 100 <= status <= 599:
            raise ModelAPIError('invalid_http_status')
        if status == 200:
            if not isinstance(result.data, dict):
                raise ModelAPIError('invalid_response_object')
            _reject_credential_metadata(result.data, key, method)
            return (result.data, attempt)
        if not (status == 429 or 500 <= status <= 599) or attempt == MAX_ATTEMPTS:
            raise ModelAPIError('http_error', status)
        sleep(2 ** (attempt - 1))
    raise AssertionError('unreachable')

def _usage(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ModelAPIError('invalid_usage')
    result = {}
    for name in _TOKEN_FIELDS:
        if name in value:
            count = value[name]
            if type(count) is not int or count < 0:
                raise ModelAPIError('invalid_usage')
            result[name] = count
    for group, names in {'prompt_tokens_details': ('cached_tokens',), 'completion_tokens_details': ('reasoning_tokens',)}.items():
        if group in value:
            details = value[group]
            if not isinstance(details, dict):
                raise ModelAPIError('invalid_usage')
            kept = {}
            for name in names:
                if name in details:
                    count = details[name]
                    if type(count) is not int or count < 0:
                        raise ModelAPIError('invalid_usage')
                    kept[name] = count
            if kept:
                result[group] = kept
    return result

def _completion(data):
    if data.get('error') is not None:
        raise ModelAPIError('provider_error')
    choices = data.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or (not isinstance(choices[0], dict)):
        raise ModelAPIError('invalid_choices')
    choice = choices[0]
    reason = choice.get('finish_reason')
    if reason != 'stop':
        known = ('length', 'content_filter', 'tool_calls', 'function_call', 'aborted', 'insufficient_system_resource')
        safe_reason = reason if reason in known else 'missing' if reason is None else 'unknown'
        raise ModelAPIError('incomplete_completion', finish_reason=safe_reason)
    message = choice.get('message')
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        raise ModelAPIError('invalid_message')
    if message.get('tool_calls') or message.get('function_call'):
        raise ModelAPIError('tool_call_rejected')
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        raise ModelAPIError('empty_content')
    actual = data.get('model')
    if not isinstance(actual, str) or not _MODEL_ID.fullmatch(actual) or '://' in actual:
        raise ModelAPIError('invalid_response_model')
    return (content, actual, _usage(data.get('usage')))

class _UsageSet(set):

    def __init__(self, lock):
        super().__init__()
        self._lock = lock

    def add(self, value):
        with self._lock:
            super().add(value)

    def clear(self):
        with self._lock:
            super().clear()

    def __iter__(self):
        with self._lock:
            return iter(tuple(set.__iter__(self)))

class APIBackend:
    is_codex = False
    is_claude = False

    def __init__(self, profile, role, log=print, *, transport=None, key_store=None, slot_root=None, sleep=time.sleep):
        if role not in ('qc', 'zh'):
            raise ConfigError('invalid_role')
        normalized = _profile(profile)
        self._profile = MappingProxyType(normalized)
        self._role, self.log = (role, log)
        self._model = normalized['provider'] + '/' + normalized['model']
        identity = {'adapter': 'model_api', 'schema': 1, 'provider': normalized['provider'], 'endpoint': _profile_urls(normalized['provider'], normalized)[0], 'model': normalized['model'], 'reasoning_effort': normalized['reasoning_effort'], 'max_tokens': normalized['max_tokens'], 'timeout': normalized['timeout'], 'role': role}
        if 'protocol' in normalized:
            identity.update({name: normalized[name] for name in _V2_FIELDS if name != 'extra_body'})
            identity.update(schema=2, extra_body=jsonlib.dumps(normalized['extra_body'], sort_keys=True, separators=(',', ':'), ensure_ascii=True))
        self._identity = ImmutableDict(identity)
        self._key_store = _store(key_store)
        self._transport, self._slot_root, self._sleep = (transport, slot_root, sleep)
        self._lock = threading.RLock()
        self.used_run, self.used_paper = (_UsageSet(self._lock), _UsageSet(self._lock))
        self._usage_history = []
        self._failure = None
        self._response_local = threading.local()

    @property
    def model(self):
        return self._model

    @property
    def profile(self):
        return MappingProxyType(copy.deepcopy(dict(self._profile)))

    @property
    def cache_identity(self):
        return self._identity

    @property
    def last_usage(self):
        with self._lock:
            return copy.deepcopy(self._usage_history[-1]) if self._usage_history else None

    @property
    def usage_history(self):
        with self._lock:
            return tuple(copy.deepcopy(self._usage_history))

    @property
    def last_response_model(self):
        return getattr(self._response_local, 'model', None)

    def _mark(self, model):
        if not isinstance(model, str):
            raise ConfigError('invalid_usage_model')
        prefix, sep, identifier = model.partition('/')
        if sep != '/' or prefix != self._profile['provider']:
            raise ConfigError('invalid_usage_model')
        _model(identifier)
        with self._lock:
            self.used_run.add(model)
            self.used_paper.add(model)

    def ask(self, prompt):
        with self._lock:
            if self._failure is not None:
                raise ModelAPIError(*self._failure)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConfigError('invalid_prompt')
        p = self._profile
        payload = {'model': p['model'], 'messages': [{'role': 'user', 'content': prompt}], 'stream': False, 'max_tokens': p['max_tokens']}
        effort = p['reasoning_effort']
        if 'protocol' in p:
            payload.update(copy.deepcopy(p['extra_body']))
            if effort != 'default':
                payload['reasoning_effort'] = effort
            if p['thinking'] != 'default':
                payload['thinking'] = {'type': p['thinking']}
        elif effort != 'default':
            if p['provider'] == 'glm':
                payload['thinking'] = {'type': 'disabled' if effort == 'none' else 'enabled'}
                if effort != 'none':
                    payload['reasoning_effort'] = effort
            else:
                payload['reasoning_effort'] = effort
        started = time.monotonic()
        try:
            data, attempts = _request(p['provider'], 'POST', payload, key_store=self._key_store, transport=self._transport, timeout=p['timeout'], slot_root=self._slot_root, sleep=self._sleep, profile=p)
            content, actual, usage = _completion(data)
        except ModelAPIError as error:
            with self._lock:
                self._failure = (error.code, error.status, error.finish_reason)
            raise
        metrics = {'provider': p['provider'], 'role': self._role, 'requested_model': p['model'], 'model': actual, 'reasoning_effort': effort, 'finish_reason': 'stop', 'usage': usage, 'attempts': attempts, 'elapsed_seconds': round(time.monotonic() - started, 3)}
        with self._lock:
            self._mark(p['provider'] + '/' + actual)
            self._usage_history.append(metrics)
        self._response_local.model = p['provider'] + '/' + actual
        return content

def create_backend(profile, role, log=print, **test_dependencies):
    return APIBackend(profile, role, log, **test_dependencies)

def test_connection(provider=None, model=None, reasoning_effort=None, *, profile=None, role='qc', key_store=None, path=None, transport=None, slot_root=None, sleep=time.sleep):
    if type(role) is not str or role not in ('qc', 'zh'):
        raise ConfigError('invalid_role')
    if profile is not None:
        if provider is not None or model is not None or reasoning_effort is not None:
            raise ConfigError('ambiguous_profile')
        selected = _profile(profile)
        store = _store(key_store, path)
    else:
        provider = _provider(provider)
        store = _store(key_store, path)
        if model is None:
            config = load_config(key_store=store)
            roles = (role, 'zh' if role == 'qc' else 'qc')
            matching = [] if config is None else [config[r] for r in roles if config[r]['provider'] == provider]
            if not matching:
                raise ConfigError('model_required')
            selected = copy.deepcopy(matching[0])
        else:
            selected = {'provider': provider, 'model': model}
        if reasoning_effort is None:
            reasoning_effort = selected.get('reasoning_effort', 'default')
        selected['reasoning_effort'] = reasoning_effort
        selected = _profile(selected)
    selected.update(timeout=min(selected['timeout'], 60), max_tokens=min(selected['max_tokens'], 1024))
    backend = APIBackend(selected, role, log=None, key_store=store, transport=transport, slot_root=slot_root, sleep=sleep)
    backend.ask('Reply with exactly OK.')
    return {'ok': True, **backend.last_usage}

def _listing_profile(provider, profile):
    if profile is None:
        return (_provider(provider), None)
    if provider is not None:
        raise ConfigError('ambiguous_profile')
    selected = _profile(profile, for_models=True)
    return (selected['provider'], selected)

def _model_details(entry):
    result = {}
    effort = entry.get('effort')
    if type(effort) is dict:
        levels = effort.get('supported_levels')
        if type(levels) is list and 0 < len(levels) <= MAX_EFFORT_LEVELS and all((type(v) is str and _EFFORT.fullmatch(v) for v in levels)):
            result['effort_levels'] = list(dict.fromkeys(levels))
            highest = model_catalog.highest_effort(result['effort_levels'])
            if highest is not None:
                result['highest_effort'] = highest
            default = effort.get('default_level')
            if type(default) is str and default in result['effort_levels']:
                result['default_effort'] = default
    for name in ('context_window', 'max_output_tokens'):
        maximum = entry.get(name)
        if type(maximum) is int and 0 < maximum <= 2 ** 31 - 1:
            result[name] = maximum
    name = entry.get('name')
    if type(name) is str and 0 < len(name) <= 256 and name.strip() and all((char.isprintable() for char in name)):
        result['display_name'] = name
    return result

def _fetch_models(provider, profile, *, key_store=None, path=None, transport=None, slot_root=None, sleep=time.sleep):
    if not _profile_urls(provider, profile)[1]:
        raise UnsupportedOperation('model_listing_not_supported')
    data, _ = _request(provider, 'GET', None, key_store=_store(key_store, path), transport=transport, timeout=min(60, profile['timeout']) if profile else 60, slot_root=slot_root, sleep=sleep, profile=profile)
    entries = data.get('data')
    if data.get('error') is not None or not isinstance(entries, list) or len(entries) > MAX_MODELS:
        raise ModelAPIError('invalid_model_list')
    models, details = ([], {})
    for entry in entries:
        value = entry.get('id') if isinstance(entry, dict) else None
        if not isinstance(value, str) or not _MODEL_ID.fullmatch(value) or '://' in value:
            raise ModelAPIError('invalid_model_list')
        if value not in models:
            models.append(value)
            kept = _model_details(entry)
            if kept:
                details[value] = kept
    return (models, details)

def list_models(provider=None, *, profile=None, key_store=None, path=None, transport=None, slot_root=None, sleep=time.sleep):
    provider, profile = _listing_profile(provider, profile)
    return _fetch_models(provider, profile, key_store=key_store, path=path, transport=transport, slot_root=slot_root, sleep=sleep)[0]

def check_tongji_model_ids(config, roles):
    models = None
    for role in roles:
        profile = config[role]
        if profile['provider'] != 'tongji' or _profile_urls('tongji', profile) != (ENDPOINTS['tongji'], MODEL_ENDPOINTS['tongji']):
            continue
        if models is None:
            models = list_models(profile=profile)
        if profile['model'] in models:
            continue
        label = '质检' if role == 'qc' else '翻译'
        case_matches = [m for m in models if m.casefold() == profile['model'].casefold()]
        if len(case_matches) == 1:
            raise ConfigError(f'同济{label}模型 ID 区分大小写，请选择 {case_matches[0]}，保存后重新预演。')
        raise ConfigError(f'当前同济 Key 的模型列表中找不到所选{label}模型，请重新获取列表并选择。')

def model_choices(provider=None, *, profile=None, key_store=None, path=None, transport=None, slot_root=None, sleep=time.sleep, catalog=None, refresh=True):
    provider, profile = _listing_profile(provider, profile)
    kind = _catalog_endpoint_kind(provider, profile)
    if not _profile_urls(provider, profile)[1]:
        if provider == 'glm' and kind in ('standard', 'coding_plan'):
            selected_catalog = model_catalog.DEFAULT_CATALOG if catalog is None else catalog
            return selected_catalog.get(kind, refresh=refresh)
        return {'provider': provider, 'models': [], 'source': 'bundled_catalog', 'endpoint_kind': kind, 'fetched_at': None, 'notice': '未配置模型列表地址；没有适用的内置文档候选（非实时）。请手填模型并测试连接；这不是账户可用清单。'}
    models, details = _fetch_models(provider, profile, key_store=key_store, path=path, transport=transport, slot_root=slot_root, sleep=sleep)
    result = {'provider': provider, 'models': models, 'source': 'provider_api', 'endpoint_kind': kind, 'fetched_at': model_catalog.utc_now(), 'reference': _profile_urls(provider, profile)[1], 'notice': '来自所配置的服务商模型列表接口；实际模型调用权限仍需测试连接确认。'}
    if provider == 'tongji':
        result['notice'] = '来自校内平台当前 Key 的模型列表，可能包含向量等非对话模型；请选择支持 Chat Completions 的 Model ID，实际调用权限仍需测试连接确认。'
    if details:
        result['details'] = details
    return result
