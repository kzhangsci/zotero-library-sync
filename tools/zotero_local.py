# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import argparse
import copy
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import unquote, urlsplit
import requests
from library_paths import REPO, SECRETS, CACHE
ORIGIN = 'http://127.0.0.1:23119'
BASE_URL = ORIGIN + '/api/users/0'
KEY_ALPHABET = '23456789ABCDEFGHIJKLMNPQRSTUVWXYZ'
KEY_RE = re.compile('[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}\\Z')
ALWAYS_ALLOW = 'Choose Always Allow in Zotero, then retry after checking the journal.'
LOGGER = logging.getLogger(__name__)

class LocalAPIError(RuntimeError):
    pass

class ProtocolError(LocalAPIError):
    pass

class TransportError(LocalAPIError):

    def __init__(self, method, *, timed_out=False):
        self.method = method.upper()
        self.read_only = self.method in {'GET', 'HEAD', 'OPTIONS'}
        self.timed_out = timed_out
        self.outcome_uncertain = not self.read_only
        failure = 'timed out' if timed_out else 'transport failed'
        if self.read_only:
            message = f'Local API read-only {self.method} {failure}; this request does not modify Zotero.'
        else:
            message = f'Local API {self.method} {failure}; write outcome may be uncertain.'
        super().__init__(message)

def _transport_timed_out(error):
    pending, seen = ([error], set())
    while pending and len(seen) < 24:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (requests.Timeout, TimeoutError)):
            return True
        pending.extend([current.__cause__, current.__context__, *current.args])
    return False

class AuthorizationRequired(LocalAPIError):
    pass

class HTTPError(LocalAPIError):

    def __init__(self, status):
        self.status = status
        super().__init__(f'Zotero local API returned HTTP {status}; no automatic retry.')

class ConflictError(HTTPError):
    pass

class SnapshotChanged(LocalAPIError):
    pass

class RecoveryRequired(LocalAPIError):

    def __init__(self, journal, rollback='not_attempted'):
        self.journal = Path(journal)
        self.rollback = rollback
        super().__init__(f'Attachment operation needs review; journal={self.journal}; rollback={rollback}.')

class BatchWriteError(LocalAPIError):

    def __init__(self, successful, failed):
        self.successful = successful
        self.failed = failed
        super().__init__('Zotero batch has failed or unaccounted-for indexes: ' + ', '.join((str(i) for i in sorted(failed))))

def _key(value):
    if not isinstance(value, str) or not KEY_RE.fullmatch(value):
        raise ValueError('Invalid Zotero item key.')
    return value

def _filename(value):
    if not isinstance(value, str) or not value or value in {'.', '..'} or re.search('[<>:"/\\\\|?*\\x00-\\x1f]', value) or value.endswith(('.', ' ')) or (len(value.encode('utf-16-le')) > 510) or re.fullmatch('(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\\..*)?', value, re.I):
        raise ValueError('Attachment filename must be a safe Windows basename.')
    return value

def _version(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError('Missing or invalid local object version.')
    return value

def _item(value, key=None):
    if not isinstance(value, dict) or not isinstance(value.get('data'), dict):
        raise ProtocolError('Expected a full Zotero item.')
    if key is not None and value.get('key') != key:
        raise ProtocolError('Returned item key does not match the request.')
    _key(value.get('key'))
    _version(value.get('version'))
    if value['data'].get('key', value['key']) != value['key']:
        raise ProtocolError('Inconsistent item keys.')
    if value['data'].get('version', value['version']) != value['version']:
        raise ProtocolError('Inconsistent item versions.')
    return value

def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _save_journal(path, state):
    _atomic_bytes(path, json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'))

@contextmanager
def _operation_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LocalAPIError('This attachment operation is already running.') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

def _dpapi(data, *, decrypt=False):
    if os.name != 'nt':
        raise LocalAPIError('Persistent Zotero authorization requires Windows DPAPI.')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise LocalAPIError('Windows DPAPI could not protect/unprotect the authorization.')
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)

class DPAPIKeyStore:

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else SECRETS
        if self.root.name != '.secrets' or self.root.resolve().name != '.secrets':
            raise ValueError('Authorization cache must be inside a .secrets directory.')

    def _path(self, server_id):
        digest = hashlib.sha256(server_id.encode()).hexdigest()
        return self.root / f'zotero-local-{digest}.dpapi'

    def load(self, server_id):
        path = self._path(server_id)
        if not path.exists():
            return None
        try:
            saved = json.loads(_dpapi(path.read_bytes(), decrypt=True))
            if saved['server_id'] != server_id or saved['remember'] is not True or (not _valid_token(saved['key'])):
                raise ValueError
            return saved['key']
        except (ValueError, KeyError, TypeError, OSError):
            raise LocalAPIError('Invalid protected authorization cache; authorize explicitly.') from None

    def save(self, server_id, key, *, remember):
        if remember is not True or not _valid_token(key):
            raise AuthorizationRequired('Single-use authorizations are never cached. ' + ALWAYS_ALLOW)
        encrypted = _dpapi(json.dumps({'server_id': server_id, 'key': key, 'remember': True}).encode())
        _atomic_bytes(self._path(server_id), encrypted)

def _valid_token(value):
    return isinstance(value, str) and re.fullmatch('[A-Za-z0-9]{32}', value) is not None

def _file_bytes(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise LocalAPIError('Attachment source is not a regular file.')
    before = path.stat()
    if before.st_size >= 4 * 1024 ** 3:
        raise LocalAPIError('Local API attachments must be smaller than 4 GiB.')
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or len(data) != before.st_size:
        raise LocalAPIError('Attachment source changed while it was read.')
    return (data, {'path': str(path), 'size': len(data), 'mtime': before.st_mtime_ns // 1000000, 'sha256': hashlib.sha256(data).hexdigest(), 'md5': hashlib.md5(data, usedforsecurity=False).hexdigest()})

class Client:
    supports_annotation_reparent = True

    def __init__(self, *, apply=False, journal_root=None, storage_root=None, session=None, timeout=(5, 45), app_name='Literature Library Local API', key_store=None, api_key=None, remember=False, snapshot_read_timeout=300, upload_timeout=(60, 180)):
        if not isinstance(timeout, (tuple, list)) or len(timeout) != 2 or any((isinstance(x, bool) or not isinstance(x, (int, float)) or (not math.isfinite(x)) or (not 0 < x <= 60) for x in timeout)):
            raise ValueError('timeout must contain bounded connect/read seconds (0, 60].')
        if isinstance(snapshot_read_timeout, bool) or not isinstance(snapshot_read_timeout, (int, float)) or (not math.isfinite(snapshot_read_timeout)) or (not 0 < snapshot_read_timeout <= 300):
            raise ValueError('snapshot_read_timeout must be finite seconds in (0, 300].')
        if not isinstance(upload_timeout, (tuple, list)) or len(upload_timeout) != 2 or any((isinstance(v, bool) or not isinstance(v, (int, float)) or (not math.isfinite(v)) or (not 0 < v <= bound) for v, bound in zip(upload_timeout, (60, 300)))):
            raise ValueError('upload_timeout must be finite send/connect <=60 and read <=300 seconds.')
        if api_key is not None and (not _valid_token(api_key)):
            raise ValueError('Invalid in-memory local API key.')
        self.apply = bool(apply)
        self.journal_root = Path(journal_root) if journal_root is not None else CACHE
        self.storage_root = Path(storage_root) if storage_root is not None else None
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self.session.auth = None
        self.session.headers.clear()
        self.session.proxies.clear()
        self.session.cookies.clear()
        if isinstance(self.session, requests.Session):
            self.session.mount('http://', requests.adapters.HTTPAdapter(max_retries=0))
        self.timeout = tuple(timeout)
        self.snapshot_timeout = (self.timeout[0], snapshot_read_timeout)
        self.upload_timeout = tuple(upload_timeout)
        self.app_name = app_name
        self.key_store = key_store if key_store is not None else DPAPIKeyStore()
        self.server_id = None
        self._api_key = api_key
        self._remember = bool(remember)
        self._loaded_key = api_key is not None

    def close(self):
        self._api_key = None
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _raw(self, method, url, *, expected=(200,), headers=None, snapshot=False, file_upload=False, **kwargs):
        if snapshot and method != 'GET':
            raise ValueError('Extended snapshot timeout is only permitted for GET.')
        if file_upload and (snapshot or method != 'POST' or (not re.fullmatch(re.escape(ORIGIN) + '/api/local/uploads/[A-Za-z0-9_-]{1,128}', url))):
            raise ValueError('Extended upload timeout is only permitted for the local byte-upload POST.')
        timeout = self.snapshot_timeout if snapshot else self.timeout
        if file_upload:
            timeout = self.upload_timeout
        self.session.cookies.clear()
        try:
            response = self.session.request(method, url, headers=headers or {}, timeout=timeout, allow_redirects=False, **kwargs)
        except (requests.RequestException, OSError, ValueError) as exc:
            raise TransportError(method, timed_out=_transport_timed_out(exc)) from None
        status = response.status_code
        if status == 401:
            self._api_key = None
            self._loaded_key = True
            raise AuthorizationRequired('Local API authorization expired or was consumed. ' + ALWAYS_ALLOW)
        if status not in expected:
            if status == 412:
                raise ConflictError(status)
            raise HTTPError(status)
        return response

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except (ValueError, TypeError):
            raise ProtocolError('Local API returned invalid JSON.') from None

    def _check_server(self, response):
        server = response.headers.get('Zotero-Server-ID')
        if not isinstance(server, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', server):
            raise ProtocolError('Zotero 10 server ID is missing or invalid.')
        if self.server_id is not None and server != self.server_id:
            self._api_key = None
            raise LocalAPIError('Zotero instance changed; discard versions and create a new client.')
        self.server_id = server

    def _ensure_server(self):
        if self.server_id is None:
            response = self._raw('GET', ORIGIN + '/api/')
            self._check_server(response)
            if response.headers.get('Zotero-API-Version', '3') != '3':
                raise ProtocolError('Only Zotero API v3 is supported.')

    @staticmethod
    def _url(path):
        if not isinstance(path, str) or re.search('[\\\\%?#\\x00-\\x20]', path):
            raise ValueError('Use an API path with a separate params mapping.')
        if ':' in path or '//' in path or any((p in {'.', '..'} for p in path.split('/'))):
            raise ValueError('Only local API paths are accepted.')
        if path.startswith('/api/'):
            return ORIGIN + path
        return BASE_URL + '/' + path.lstrip('/')

    def _require_key(self):
        if not self.apply:
            raise LocalAPIError('Internal write attempted without apply=True.')
        self._ensure_server()
        if self._api_key is None and (not self._loaded_key):
            self._api_key = self.key_store.load(self.server_id)
            self._remember = self._api_key is not None
            self._loaded_key = True
        if self._api_key is None:
            raise AuthorizationRequired('Explicit authorization is required. ' + ALWAYS_ALLOW)

    def _api(self, method, path, *, expected=(200,), headers=None, authorize=False, **kwargs):
        url = self._url(path)
        self._ensure_server()
        request_headers = {'Zotero-API-Version': '3', 'Zotero-Server-ID': self.server_id}
        if headers:
            request_headers.update(headers)
        mutation = method != 'GET'
        if mutation:
            if not self.apply:
                raise LocalAPIError('Internal write attempted without apply=True.')
            if not authorize:
                self._require_key()
                request_headers['Zotero-API-Key'] = self._api_key
        try:
            response = self._raw(method, url, expected=expected, headers=request_headers, **kwargs)
            self._check_server(response)
            return response
        finally:
            if mutation and (not authorize) and (not self._remember):
                self._api_key = None

    def get(self, path, params=None):
        response = self._get_response(path, params)
        if 'json' in response.headers.get('Content-Type', 'application/json').lower():
            return self._json(response)
        return response.text

    def _get_response(self, path, params=None, *, snapshot=False):
        if params and any((str(k).lower() in {'key', 'token', 'api_key', 'authorization'} for k in params)):
            raise ValueError('Credentials are not allowed in URL parameters.')
        return self._api('GET', path, params=params, snapshot=snapshot)

    def authorize(self):
        if not self.apply:
            return {'dry_run': True, 'action': 'authorize', 'message': ALWAYS_ALLOW}
        response = self._api('POST', '/api/local/authorize', authorize=True, json={'appName': self.app_name})
        grant = self._json(response)
        if not isinstance(grant, dict) or not _valid_token(grant.get('key')):
            raise ProtocolError('Invalid local API authorization response.')
        if grant.get('remember') is not True:
            self._api_key = None
            self._loaded_key = True
            raise AuthorizationRequired('Single-use authorization discarded. ' + ALWAYS_ALLOW)
        self.key_store.save(self.server_id, grant['key'], remember=True)
        self._api_key, self._remember, self._loaded_key = (grant['key'], True, True)
        return {'authorized': True, 'remember': True, 'persisted': True, 'server_id': self.server_id}

    @staticmethod
    def _response_version(response):
        value = response.headers.get('Last-Modified-Version', '')
        if not re.fullmatch('[0-9]+', str(value)):
            raise ProtocolError('Missing local version header.')
        return int(value)

    def _patch(self, key, changes, version):
        response = self._api('PATCH', 'items/' + key, expected=(204,), headers={'If-Unmodified-Since-Version': str(version)}, json=changes)
        return self._response_version(response)

    def patch_item(self, key, changes, version):
        key, version = (_key(key), _version(version))
        if not isinstance(changes, dict) or not changes or {'key', 'version'} & changes.keys():
            raise ValueError('PATCH requires changed fields without key/version overrides.')
        if not self.apply:
            return {'dry_run': True, 'action': 'patch_item', 'key': key, 'version': version, 'changes': copy.deepcopy(changes)}
        self._patch(key, changes, version)
        return _item(self.get('items/' + key), key)

    def create_items(self, payload, *, write_token=None):
        return self._create_objects('items', payload, write_token=write_token)

    def create_collections(self, payload, *, write_token=None):
        return self._create_objects('collections', payload, write_token=write_token)

    def _create_objects(self, endpoint, payload, *, write_token=None):
        if not isinstance(payload, list) or not 1 <= len(payload) <= 50:
            raise ValueError('Batch creation requires an array of 1..50 objects.')
        if any((not isinstance(item, dict) for item in payload)):
            raise ValueError('Each item must be an object.')
        payload = copy.deepcopy(payload)
        for value in payload:
            if 'key' in value:
                _key(value['key'])
                value.setdefault('version', 0)
                _version(value['version'])
        if write_token is not None and (not _valid_token(write_token)):
            raise ValueError('write_token must contain 32 alphanumeric characters.')
        if not self.apply:
            return {'dry_run': True, 'action': 'create_' + endpoint, endpoint: copy.deepcopy(payload)}
        token = write_token or uuid.uuid4().hex
        response = self._api('POST', endpoint, json=payload, headers={'Zotero-Write-Token': token})
        result = self._json(response)
        if not isinstance(result, dict):
            raise ProtocolError('Invalid batch result; reconcile before retrying.')
        groups = [result.get(name, {}) for name in ('successful', 'unchanged', 'failed')]
        if any((not isinstance(group, dict) for group in groups)):
            raise ProtocolError('Invalid batch indexes; reconcile before retrying.')
        success, unchanged, failures = groups
        expected_indexes = {str(i) for i in range(len(payload))}
        if any((set(group) - expected_indexes for group in groups)):
            raise ProtocolError('Unexpected batch indexes; reconcile before retrying.')
        saved, failed = ({}, {})
        for i in range(len(payload)):
            index = str(i)
            if sum((index in group for group in groups)) != 1 or index in failures:
                entry = failures.get(index, {})
                code = entry.get('code') if isinstance(entry, dict) else None
                failed[i] = code if isinstance(code, int) else None
                continue
            if index in success:
                saved[i] = _item(success[index], payload[i].get('key'))
            else:
                key = _key(unchanged[index])
                if payload[i].get('key', key) != key:
                    raise ProtocolError('Batch unchanged key mismatch.')
                saved[i] = _item(self.get(endpoint + '/' + key), key)
        if failed:
            raise BatchWriteError(saved, failed)
        return [saved[i] for i in range(len(payload))]

    def _library_version(self):
        return self._response_version(self._get_response('items', {'limit': 1, 'includeTrashed': 1}))

    def _pages(self, path, version, page_size=100, extra=None, *, max_pages=10000, snapshot=False):
        out, seen, start, total = ([], set(), 0, None)
        resource = path if path in {'items', 'collections'} else 'list'
        for page in range(1, max_pages + 1):
            started = time.monotonic()
            LOGGER.info('Local API pagination request resource=%s page=%d start=%d limit=%d version=%d', resource, page, start, page_size, version)
            try:
                response = self._get_response(path, {**(extra or {}), 'limit': page_size, 'start': start}, snapshot=snapshot)
                if self._response_version(response) != version:
                    raise SnapshotChanged('Library changed during pagination.')
                batch = self._json(response)
                header = response.headers.get('Total-Results')
                if not isinstance(batch, list) or header is None or (not re.fullmatch('[0-9]+', str(header))):
                    raise ProtocolError('Missing list/total in paginated response.')
                if total is not None and total != int(header):
                    raise SnapshotChanged('Result count changed during pagination.')
                total = int(header)
                for value in batch:
                    key = _item(value)['key']
                    if value['version'] > version:
                        raise SnapshotChanged('Object version is newer than the library page.')
                    if key in seen:
                        raise ProtocolError('Duplicate or missing key in snapshot.')
                    seen.add(key)
                received = start + len(batch)
                if received > total or (not batch and received != total):
                    raise ProtocolError('Incomplete paginated response.')
            except (LocalAPIError, ValueError) as exc:
                LOGGER.warning('Local API pagination failed resource=%s page=%d start=%d limit=%d version=%d elapsed=%.3fs error=%s timed_out=%s', resource, page, start, page_size, version, time.monotonic() - started, type(exc).__name__, isinstance(exc, TransportError) and exc.timed_out)
                raise
            LOGGER.info('Local API pagination verified resource=%s page=%d start=%d count=%d received=%d total=%d version=%d elapsed=%.3fs', resource, page, start, len(batch), received, total, version, time.monotonic() - started)
            out.extend(batch)
            start = received
            if start == total:
                return out
        LOGGER.warning('Local API pagination exhausted resource=%s pages=%d received=%d total=%s', resource, max_pages, start, total)
        raise ProtocolError('Pagination exceeded its bounded page limit.')

    def _unpaginated(self, path, response, version, extra=None, *, snapshot=True, max_pages=10000):
        if self._response_version(response) != version:
            raise SnapshotChanged('Library changed during full-list retrieval.')
        values = self._json(response)
        total = response.headers.get('Total-Results')
        if not isinstance(values, list) or total is None or (not str(total).isdigit()):
            raise ProtocolError('Missing list/total in unpaginated response.')
        keys = [_item(value)['key'] for value in values]
        if len(set(keys)) != len(keys):
            raise ProtocolError('Duplicate key in unpaginated snapshot.')
        if any((value['version'] > version for value in values)):
            raise SnapshotChanged('Object version is newer than the library response.')
        if len(values) > int(total):
            raise ProtocolError('Unpaginated response exceeds Total-Results.')
        if len(values) < int(total):
            return self._pages(path, version, extra=extra, snapshot=snapshot, max_pages=max_pages)
        return values

    def _fast_snapshot(self, attempts):
        if isinstance(attempts, bool) or not isinstance(attempts, int) or (not 1 <= attempts <= 10):
            raise ValueError('attempts must be in 1..10.')
        for _ in range(attempts):
            try:
                response = self._get_response('items', {'includeTrashed': 0}, snapshot=True)
                version = self._response_version(response)
                items = self._unpaginated('items', response, version, {'includeTrashed': 0})
                response = self._get_response('collections', snapshot=True)
                collections = self._unpaginated('collections', response, version)
                if self._library_version() != version:
                    raise SnapshotChanged('Library changed before snapshot completed.')
                return {'server_id': self.server_id, 'library_version': version, 'items': items, 'collections': collections}
            except SnapshotChanged:
                continue
        raise SnapshotChanged('Library did not stabilize within the bounded attempts.')

    def _stable(self, read, attempts):
        if isinstance(attempts, bool) or not isinstance(attempts, int) or (not 1 <= attempts <= 10):
            raise ValueError('attempts must be in 1..10.')
        for attempt in range(1, attempts + 1):
            stage = 'initial_version'
            started = time.monotonic()
            try:
                LOGGER.info('Local API stable read attempt=%d/%d stage=%s', attempt, attempts, stage)
                version = self._library_version()
                stage = 'pages'
                result = read(version)
                stage = 'final_version'
                LOGGER.info('Local API stable read attempt=%d/%d stage=%s version=%d', attempt, attempts, stage, version)
                final_version = self._library_version()
                if final_version != version:
                    raise SnapshotChanged('Library changed before snapshot completed.')
                LOGGER.info('Local API stable read complete attempt=%d/%d version=%d elapsed=%.3fs', attempt, attempts, version, time.monotonic() - started)
                return {'server_id': self.server_id, 'library_version': version, **result}
            except SnapshotChanged:
                LOGGER.warning('Local API stable read changed attempt=%d/%d stage=%s retry=%s', attempt, attempts, stage, attempt < attempts)
                continue
            except LocalAPIError as exc:
                LOGGER.warning('Local API stable read failed attempt=%d/%d stage=%s elapsed=%.3fs error=%s', attempt, attempts, stage, time.monotonic() - started, type(exc).__name__)
                raise
        raise SnapshotChanged('Library did not stabilize within the bounded attempts.')

    def snapshot(self, *, page_size=None, attempts=3):
        if page_size is None:
            result = self._fast_snapshot(attempts)
        else:
            if isinstance(page_size, bool) or not isinstance(page_size, int) or (not 1 <= page_size <= 100):
                raise ValueError('page_size must be in 1..100.')
            result = self._stable(lambda version: {'items': self._pages('items', version, page_size, {'includeTrashed': 0}, snapshot=True), 'collections': self._pages('collections', version, page_size, snapshot=True)}, attempts)
        result['items'] = [value for value in result['items'] if not value['data'].get('deleted')]
        return result

    def stable_snapshot(self, *, page_size=100, attempts=3):
        result = self.snapshot(page_size=page_size, attempts=attempts)
        return {'time': datetime.now(timezone.utc).isoformat(), 'items': result['items'], 'collections': result['collections'], 'version': result['library_version'], 'server_id': result['server_id']}

    def list_all(self, route, params=None, *, page_size=100, attempts=3, max_pages=1000):
        if isinstance(page_size, bool) or not isinstance(page_size, int) or (not 1 <= page_size <= 100):
            raise ValueError('page_size must be in 1..100.')
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or (not 1 <= max_pages <= 10000):
            raise ValueError('max_pages must be in 1..10000.')
        if params is not None and (not isinstance(params, dict)):
            raise ValueError('params must be a mapping of API filters.')
        if params and {'start', 'limit'} & params.keys():
            raise ValueError('list_all owns pagination; use page_size/max_pages, not start/limit.')
        self._url(route)
        filters = copy.deepcopy(params) if params else None
        return self._stable(lambda version: {'objects': self._pages(route, version, page_size, filters, max_pages=max_pages)}, attempts)['objects']

    def parent_children(self, key, *, attempts=3):
        key = _key(key)

        def read(version):
            parent = _item(self.get('items/' + key), key)
            if parent['data'].get('deleted'):
                raise LocalAPIError('Requested parent is deleted; no active parent/children can be returned.')
            children = self._pages(f'items/{key}/children', version, extra={'includeTrashed': 0})
            if any((child.get('data', {}).get('parentItem') != key for child in children)):
                raise ProtocolError('Child has an unexpected parent.')
            if parent['data'].get('itemType') == 'attachment':
                params = {'itemType': 'annotation', 'includeTrashed': 0}
                response = self._get_response('items', params)
                annotations = self._unpaginated('items', response, version, params, snapshot=False, max_pages=1000)
                for annotation in annotations:
                    data = annotation['data']
                    if data.get('itemType') != 'annotation' or not isinstance(data.get('parentItem'), str) or (not KEY_RE.fullmatch(data['parentItem'])):
                        raise ProtocolError('Invalid annotation-type list response.')
                combined = {child['key']: child for child in children}
                for annotation in annotations:
                    if annotation['data']['parentItem'] != key:
                        continue
                    duplicate = combined.get(annotation['key'])
                    if duplicate is not None and duplicate['data'] != annotation['data']:
                        raise ProtocolError('Children and annotation list disagree on an annotation.')
                    combined[annotation['key']] = annotation
                children = list(combined.values())
            return {'parent': parent, 'children': [child for child in children if not child['data'].get('deleted')]}
        return self._stable(read, attempts)

    def _storage(self, override=None):
        root = Path(override) if override is not None else self.storage_root
        if root is None:
            if __package__:
                from .common import zotero_data_dir
            else:
                from common import zotero_data_dir
            root = zotero_data_dir() / 'storage'
        return root.resolve(strict=True)

    @staticmethod
    def _destination(root, key, filename):
        parent = (root / _key(key)).resolve()
        target = (parent / _filename(filename)).resolve()
        if parent.parent != root or target.parent != parent:
            raise LocalAPIError('Attachment path escapes its storage directory.')
        return target

    def file_path(self, key, *, storage_root=None, filename=None):
        key = _key(key)
        if filename is None:
            filename = _item(self.get('items/' + key), key)['data'].get('filename')
        expected = self._destination(self._storage(storage_root), key, filename)
        response = self._get_response(f'items/{key}/file/view/url')
        try:
            uri = urlsplit(response.text.strip())
            if uri.scheme != 'file' or uri.netloc not in {'', 'localhost'} or uri.query or uri.fragment:
                raise ValueError
            decoded = unquote(uri.path)
            if os.name == 'nt' and re.match('^/[A-Za-z]:/', decoded):
                decoded = decoded[1:]
            actual = Path(decoded).resolve(strict=True)
            if actual != expected or not actual.is_file():
                raise ValueError
        except (ValueError, OSError):
            raise LocalAPIError('Attachment file URL is missing or outside its expected storage path.') from None
        return actual

    def _verify_file(self, key, filename, info, root):
        current = _item(self.get('items/' + key), key)
        self._stored(current)
        if current['data'].get('filename') != filename:
            raise LocalAPIError('Attachment filename changed before verification.')
        _, verified = _file_bytes(self.file_path(key, storage_root=root, filename=filename))
        if any((verified[field] != info[field] for field in ('sha256', 'size'))):
            raise LocalAPIError('Attachment SHA256/size verification failed.')
        latest = _item(self.get('items/' + key), key)
        if latest['version'] != current['version'] or latest['data'].get('filename') != filename:
            raise LocalAPIError('Attachment changed while its uploaded file was verified.')
        return latest

    @staticmethod
    def _stored(item):
        if item['data'].get('itemType') != 'attachment' or item['data'].get('linkMode') not in {'imported_file', 'imported_url'}:
            raise ValueError('Only stored-file attachments support this operation.')

    @staticmethod
    def _precondition(item):
        md5 = item['data'].get('md5')
        if md5 in (None, ''):
            return {'If-None-Match': '*'}
        if not isinstance(md5, str) or not re.fullmatch('[0-9a-fA-F]{32}', md5):
            raise ProtocolError('Invalid synced attachment MD5.')
        return {'If-Match': md5}

    @staticmethod
    def _upload_url(value, upload_key):
        try:
            uri = urlsplit(value)
            if uri.scheme != 'http' or uri.hostname not in {'127.0.0.1', 'localhost', '::1'} or uri.port != 23119 or (uri.username is not None) or (uri.password is not None) or uri.query or uri.fragment or (not isinstance(upload_key, str)) or (not re.fullmatch('[A-Za-z0-9_-]{1,128}', upload_key)) or (uri.path != '/api/local/uploads/' + upload_key):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ProtocolError('Rejected nonlocal or malformed upload URL.') from None
        return ORIGIN + uri.path

    def _unchanged_original(self, original, info, root):
        key, filename = (original['key'], original['data']['filename'])
        current = _item(self.get('items/' + key), key)
        if current['version'] != original['version'] or current['data'].get('filename') != filename:
            raise LocalAPIError('Attachment changed during upload-precondition discovery.')
        _, actual = _file_bytes(self.file_path(key, storage_root=root, filename=filename))
        if any((actual[field] != info[field] for field in ('size', 'sha256', 'mtime'))):
            raise LocalAPIError('Original file changed during upload-precondition discovery.')
        current = _item(self.get('items/' + key), key)
        if current['version'] != original['version'] or current['data'].get('filename') != filename:
            raise LocalAPIError('Attachment changed while its original file was verified.')
        return current

    def _discover_precondition(self, original, info, root, state, journal):
        key = original['key']
        fields = {'md5': info['md5'], 'filename': original['data']['filename'], 'filesize': str(info['size']), 'mtime': str(info['mtime'])}
        conditions = [{'If-None-Match': '*'}, {'If-Match': info['md5']}]
        for index, condition in enumerate(conditions):
            self._unchanged_original(original, info, root)
            attempt = {'condition': condition, 'status': 'attempted'}
            state.setdefault('preflight_attempts', []).append(attempt)
            state['stage'] = 'preflight_attempted'
            _save_journal(journal, state)
            try:
                response = self._api('POST', f'items/{key}/file', headers=condition, data=fields)
            except ConflictError:
                attempt['status'] = 412
                _save_journal(journal, state)
                if index == 0:
                    continue
                raise
            grant = self._json(response)
            if not isinstance(grant, dict) or type(grant.get('exists')) is not int or grant['exists'] != 1:
                raise ProtocolError('Old-file preflight did not return exists:1; filename was not changed.')
            attempt['status'] = 'exists:1'
            current = _item(self.get('items/' + key), key)
            reply_version = self._response_version(response) if 'Last-Modified-Version' in response.headers else None
            if current['version'] < original['version']:
                raise LocalAPIError('Item version moved backwards during old-file preflight.')
            if reply_version is not None and current['version'] not in {original['version'], reply_version}:
                raise LocalAPIError('Concurrent item version after old-file preflight.')
            ignored = {'version', 'md5', 'mtime', 'dateModified'}
            old_fields = {k: v for k, v in original['data'].items() if k not in ignored}
            new_fields = {k: v for k, v in current['data'].items() if k not in ignored}
            if old_fields != new_fields:
                raise LocalAPIError('Attachment metadata changed during old-file preflight.')
            self._unchanged_original(current, info, root)
            attempt['response_version'] = reply_version
            attempt['verified_item_version'] = current['version']
            state.update(stage='preflight_verified', preflight_item=current, file_precondition={'If-Match': info['md5']}, last_write_version=current['version'])
            _save_journal(journal, state)
            return current
        raise ProtocolError('Upload precondition could not be established.')

    def _upload(self, item, data, info, filename, progress, *, precondition=None):
        self._stored(item)
        endpoint = f"items/{item['key']}/file"
        condition = dict(precondition) if precondition is not None else self._precondition(item)
        progress('upload_authorization_attempted', None)
        response = self._api('POST', endpoint, headers=condition, data={'md5': info['md5'], 'filename': filename, 'filesize': str(info['size']), 'mtime': str(info['mtime'])})
        grant = self._json(response)
        if not isinstance(grant, dict):
            raise ProtocolError('Invalid upload authorization response.')
        if grant.get('exists') == 1:
            progress('file_exists', None)
            return
        url = self._upload_url(grant.get('url'), grant.get('uploadKey'))
        if grant.get('prefix', '') != '' or grant.get('suffix', '') != '':
            raise ProtocolError('Unexpected multipart data in local upload response.')
        content_type = grant.get('contentType', 'application/octet-stream')
        if not isinstance(content_type, str) or re.search('[\\r\\n]', content_type):
            raise ProtocolError('Invalid upload content type.')
        progress('upload_bytes_attempted', None)
        self._raw('POST', url, expected=(201,), data=data, headers={'Content-Type': content_type}, file_upload=True)
        progress('bytes_uploaded', None)
        progress('register_attempted', None)
        response = self._api('POST', endpoint, expected=(204,), headers=condition, data={'upload': grant['uploadKey']})
        progress('registered', self._response_version(response))

    def _journal_path(self, operation, identity, override):
        self._ensure_server()
        partition = hashlib.sha256(self.server_id.encode()).hexdigest()[:24]
        digest = hashlib.sha256(identity.encode()).hexdigest()
        root = Path(override) if override is not None else self.journal_root
        return root / 'zotero_local' / partition / f'{operation}-{digest}.json'

    @staticmethod
    def _read_journal(path):
        try:
            result = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(result, dict) or result.get('schema') != 1:
                raise ValueError
            return result
        except (ValueError, OSError):
            raise LocalAPIError('Invalid attachment journal; manual review required.') from None

    @staticmethod
    def _finish(item, path, info):
        result = copy.deepcopy(item)
        result['_local_api'] = {'journal': str(path) if path else None, 'sha256': info['sha256'], 'size': info['size'], 'verified': True}
        return result

    def import_attachment(self, parent, source, filename, title, idempotency_key, *, journal_root=None, storage_root=None, existing_only=False):
        parent, filename = (_key(parent), _filename(filename))
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError('A nonempty idempotency_key is required.')
        if not isinstance(title, str):
            raise ValueError('Attachment title must be text.')
        data, info = _file_bytes(source)
        root = self._storage(storage_root)
        path = self._journal_path('import', idempotency_key, journal_root)
        digest = hashlib.sha256((self.server_id + '\x00' + idempotency_key).encode()).digest()
        intended_key = ''.join((KEY_ALPHABET[b & 31] for b in digest[:8]))
        intent = {'parent': parent, 'filename': filename, 'title': title, 'sha256': info['sha256'], 'size': info['size']}
        if not self.apply:
            return {'dry_run': True, 'action': 'import_attachment', 'key': intended_key, **intent}
        self._require_key()
        with _operation_lock(path.with_suffix('.lock')):
            state = self._read_journal(path) if path.exists() else None
            if existing_only and state is None:
                raise LocalAPIError('Recovery requires an existing matching import journal.')
            if state is not None and (state.get('intent') != intent or state.get('key') != intended_key or state.get('server_id') != self.server_id):
                raise LocalAPIError('Idempotency key was already used for a different attachment intent.')
            parent_item = _item(self.get('items/' + parent), parent)
            if parent_item['data'].get('itemType') in {'attachment', 'note', 'annotation'} or parent_item['data'].get('deleted'):
                raise ValueError('Attachment parent must be an existing regular item.')
            try:
                existing = _item(self.get('items/' + intended_key), intended_key)
            except HTTPError as exc:
                if exc.status != 404:
                    raise
                existing = None
            if existing_only and existing is None:
                raise RecoveryRequired(path, 'existing_attachment_missing_no_POST')
            if state is None:
                if existing is not None:
                    raise LocalAPIError('Intended attachment key is occupied without a matching journal.')
                state = {'schema': 1, 'operation': 'import', 'server_id': self.server_id, 'key': intended_key, 'intent': intent, 'before_parent': parent_item, 'source': info, 'stage': 'prepared', 'create_attempted': False}
                _save_journal(path, state)
            if existing is None and state['create_attempted']:
                raise RecoveryRequired(path, 'creation_uncertain_no_second_POST')
            try:
                if existing is None:
                    state.update(stage='create_attempted', create_attempted=True)
                    _save_journal(path, state)
                    payload = {'key': intended_key, 'itemType': 'attachment', 'parentItem': parent, 'linkMode': 'imported_file', 'filename': filename, 'title': title, 'contentType': mimetypes.guess_type(filename)[0] or 'application/octet-stream', 'tags': [], 'collections': [], 'relations': {}}
                    try:
                        existing = self.create_items([payload], write_token=digest.hex()[:32])[0]
                    except LocalAPIError:
                        existing = _item(self.get('items/' + intended_key), intended_key)
                self._stored(existing)
                if existing['data'].get('parentItem') != parent or existing['data'].get('filename') != filename or existing['data'].get('title') != title or existing['data'].get('deleted'):
                    raise LocalAPIError('Existing attachment no longer matches the journal intent.')
                destination = self._destination(root, intended_key, filename)
                if destination.exists():
                    _, present = _file_bytes(destination)
                    if present['sha256'] != info['sha256'] or present['size'] != info['size']:
                        raise LocalAPIError('Destination has different content; refusing to overwrite.')
                    result = self._verify_file(intended_key, filename, info, root)
                else:

                    def progress(stage, version):
                        state.update(stage=stage, last_write_version=version)
                        state.setdefault('phase_times', {})[stage] = time.time()
                        if stage in {'registered', 'file_exists'}:
                            state['known_synced_md5'] = info['md5']
                        _save_journal(path, state)
                    known_hash = state.get('known_synced_md5')
                    if known_hash is not None:
                        if known_hash != info['md5']:
                            raise LocalAPIError('Journal synced hash differs from the attachment intent.')
                        condition = {'If-Match': known_hash}
                    else:
                        condition = self._precondition(existing)
                    self._upload(existing, data, info, filename, progress, precondition=condition)
                    result = self._verify_file(intended_key, filename, info, root)
                state.update(stage='complete', after=result)
                if state.get('error_type'):
                    state['recovered_error_type'] = state.pop('error_type')
                    if 'error_context' in state:
                        state['recovered_error_context'] = state.pop('error_context')
                _save_journal(path, state)
                return self._finish(result, path, info)
            except (LocalAPIError, OSError, ValueError) as exc:
                state['error_type'] = type(exc).__name__
                if isinstance(exc, TransportError):
                    state['error_context'] = {'method': exc.method, 'timed_out': exc.timed_out, 'outcome_uncertain': exc.outcome_uncertain}
                _save_journal(path, state)
                raise RecoveryRequired(path, 'attachment_retained') from None

    def _rename_retry_evidence(self, state, path, root):
        key, filename = (_key(state.get('key')), _filename(state.get('filename')))
        expected = self._journal_path('rename', key + '\x00' + filename, None)
        if Path(path).resolve() != expected.resolve() or state.get('server_id') != self.server_id or state.get('operation') != 'rename' or (state.get('stage') != 'failed') or (state.get('error_type') != 'TransportError') or (state.get('rollback') not in {'restored', 'already_original'}):
            raise LocalAPIError('Recovery requires the matching rolled-back transport-failure journal.')
        before = state['before']['parent']
        old_name = _filename(before['data'].get('filename'))
        old_path = self._destination(root, key, old_name)
        if Path(state['source']['path']).resolve() != old_path.resolve():
            raise LocalAPIError('Journal source is not the original stored attachment path.')
        backup = Path(path).with_suffix('.original')
        if Path(state['original_bytes']).resolve() != backup.resolve():
            raise LocalAPIError('Original-byte backup path differs from the journal identity.')
        for source in (old_path, backup):
            _, info = _file_bytes(source)
            if any((info[k] != state['source'][k] for k in ('size', 'sha256', 'md5'))):
                raise LocalAPIError('Original PDF or original-byte backup changed; recovery refused.')
        current = self.parent_children(key)
        self._stored(current['parent'])
        ignored = {'version', 'dateModified', 'md5', 'mtime'}
        clean = lambda item: {k: v for k, v in item['data'].items() if k not in ignored}
        if clean(before) != clean(current['parent']) or current['parent']['data'].get('deleted'):
            raise LocalAPIError('Rolled-back attachment metadata changed; recovery refused.')
        child_map = lambda family: {i['key']: i['data'] for i in family['children']}
        if child_map(current) != child_map(state['before']):
            raise LocalAPIError('Reader annotations changed; recovery refused.')
        self._verify_file(key, old_name, state['source'], root)
        family = self.parent_children(before['data']['parentItem'])
        if family['parent']['data'].get('deleted'):
            raise LocalAPIError('Parent was deleted; recovery refused.')
        for child in family['children']:
            if child['key'] != key and child['data'].get('filename') == filename:
                raise LocalAPIError('Another attachment occupies the rename target.')
        destination = self._destination(root, key, filename)
        if destination.exists():
            _, target = _file_bytes(destination)
            if any((target[k] != state['source'][k] for k in ('size', 'sha256'))):
                raise LocalAPIError('Rename target contains different bytes; recovery refused.')
        return {'attachment': {'parent': current['parent'], 'children': current['children']}, 'parent_key': family['parent']['key'], 'members': sorted((i['key'] for i in family['children'])), 'source_sha256': state['source']['sha256'], 'source_mtime': old_path.stat().st_mtime_ns}

    def approve_rename_retry(self, journal, key):
        path, key = (Path(journal).resolve(strict=True), _key(key))
        root = self._storage()
        with _operation_lock(path.with_suffix('.lock')) if self.apply else nullcontext():
            state = self._read_journal(path)
            if state.get('key') != key:
                raise LocalAPIError('Recovery attachment key differs from the journal.')
            evidence = self._rename_retry_evidence(state, path, root)
            if self.apply:
                state['retry_authorization'] = {'approved_at': time.time(), 'evidence': evidence}
                _save_journal(path, state)
            return {'dry_run': not self.apply, 'action': 'authorize_one_rename_retry', 'key': key, 'parent': evidence['parent_key'], 'filename': state['filename'], 'sha256': evidence['source_sha256'], 'members': evidence['members'], 'expires_in_seconds': 600, 'zotero_writes': 0}

    def _rollback_filename(self, state, path, root):
        try:
            current = _item(self.get('items/' + state['key']), state['key'])
            before = state['before']['parent']
            if current['data'].get('filename') == before['data']['filename']:
                return 'already_original'
            if current['version'] != state.get('last_write_version') or current['data'].get('filename') != state['filename']:
                return 'skipped_concurrent_or_uncertain_version'
            _, retained = _file_bytes(state['source']['path'])
            if retained['sha256'] != state['source']['sha256']:
                return 'skipped_original_file_changed'
            state['rollback_attempted_version'] = current['version']
            _save_journal(path, state)
            self._patch(state['key'], {'filename': before['data']['filename']}, current['version'])
            self._verify_file(state['key'], before['data']['filename'], state['source'], root)
            return 'restored'
        except (LocalAPIError, ValueError, OSError):
            return 'failed_or_uncertain'

    def rename_attachment(self, key, filename, *, journal_root=None, storage_root=None):
        key, filename = (_key(key), _filename(filename))
        root = self._storage(storage_root)
        path = self._journal_path('rename', key + '\x00' + filename, journal_root)
        if not self.apply:
            before = self.parent_children(key)
            self._stored(before['parent'])
            return {'dry_run': True, 'action': 'rename_attachment', 'key': key, 'filename': filename, 'before': before}
        self._require_key()
        with _operation_lock(path.with_suffix('.lock')):
            previous = self._read_journal(path) if path.exists() else None
            history = []
            if previous is not None:
                if previous.get('stage') == 'complete':
                    result = self._verify_file(key, filename, previous['source'], root)
                    return self._finish(result, path, previous['source'])
                permit = previous.get('retry_authorization', {})
                approved = permit.get('approved_at', 0)
                if type(approved) not in (int, float) or not math.isfinite(approved) or (not 0 <= time.time() - approved <= 600):
                    raise RecoveryRequired(path, previous.get('rollback', 'unfinished_rename'))
                evidence = self._rename_retry_evidence(previous, path, root)
                if evidence != permit.get('evidence'):
                    raise LocalAPIError('Attachment changed after explicit retry approval.')
                previous = copy.deepcopy(previous)
                history = previous.pop('attempts', [])
                previous.pop('retry_authorization', None)
                history = [*history, previous]
            before = self.parent_children(key)
            original = before['parent']
            self._stored(original)
            old_filename = _filename(original['data'].get('filename'))
            old_path = self.file_path(key, storage_root=root, filename=old_filename)
            data, info = _file_bytes(old_path)
            if filename == old_filename:
                return self._finish(original, None, info)
            destination = self._destination(root, key, filename)
            if destination == old_path or filename.casefold() == old_filename.casefold():
                raise LocalAPIError('Case-only renames cannot preserve a distinct old physical file.')
            if destination.exists():
                _, present = _file_bytes(destination)
                if any((present[field] != info[field] for field in ('size', 'sha256'))):
                    raise LocalAPIError('Rename destination exists with different content.')
            backup = path.with_suffix('.original')
            _atomic_bytes(backup, data)
            state = {'schema': 1, 'operation': 'rename', 'server_id': self.server_id, 'key': key, 'filename': filename, 'before': before, 'source': info, 'original_bytes': str(backup), 'file_precondition': None, 'stage': 'prepared', 'last_write_version': None}
            if history:
                state['attempts'] = history
            _save_journal(path, state)
            try:
                preflight_item = self._discover_precondition(original, info, root, state, path)
                state['stage'] = 'patch_attempted'
                _save_journal(path, state)
                written_version = self._patch(key, {'filename': filename}, preflight_item['version'])
                state.update(stage='patched', last_write_version=written_version)
                _save_journal(path, state)
                patched = _item(self.get('items/' + key), key)
                if patched['version'] != written_version or patched['data'].get('filename') != filename:
                    raise LocalAPIError('Attachment changed after filename PATCH.')

                def progress(stage, version):
                    state['stage'] = stage
                    state.setdefault('phase_times', {})[stage] = time.time()
                    if version is not None:
                        state['last_write_version'] = version
                    _save_journal(path, state)
                self._upload(patched, data, info, filename, progress, precondition=state['file_precondition'])
                result = self._verify_file(key, filename, info, root)
                after = self.parent_children(key)
                old_children = {child['key']: child['data'] for child in before['children']}
                new_children = {child['key']: child['data'] for child in after['children']}
                if new_children != old_children:
                    raise LocalAPIError('Attachment children changed during rename.')
                if after['parent']['version'] != result['version']:
                    raise LocalAPIError('Attachment changed during final verification.')
                _, retained = _file_bytes(old_path)
                if retained['sha256'] != info['sha256']:
                    raise LocalAPIError('Retained original file changed during rename.')
                state.update(stage='complete', after=after, old_physical_file_retained=True)
                _save_journal(path, state)
                return self._finish(result, path, info)
            except (LocalAPIError, OSError, ValueError) as exc:
                state['error_type'] = type(exc).__name__
                state['failed_stage'] = state['stage']
                if isinstance(exc, TransportError):
                    state['error_context'] = {'method': exc.method, 'timed_out': exc.timed_out, 'outcome_uncertain': exc.outcome_uncertain}
                state['rollback'] = self._rollback_filename(state, path, root)
                state['stage'] = 'failed'
                _save_journal(path, state)
                raise RecoveryRequired(path, state['rollback']) from None

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--authorize', action='store_true', help='Request Always Allow; requires --apply')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Enable explicitly requested mutations')
    mode.add_argument('--dry-run', action='store_true', help='Default: never authorize or write files')
    parser.add_argument('--limit', type=int, default=0, help='Limit displayed snapshot keys; 0 means all')
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
    try:
        with Client(apply=args.apply) as client:
            if args.authorize:
                print(ALWAYS_ALLOW, flush=True)
                print(json.dumps(client.authorize(), ensure_ascii=False))
            else:
                snapshot = client.snapshot()
                items = snapshot['items'][:args.limit] if args.limit else snapshot['items']
                print(json.dumps({'server_id': snapshot['server_id'], 'library_version': snapshot['library_version'], 'item_count': len(snapshot['items']), 'collection_count': len(snapshot['collections']), 'keys': [item['key'] for item in items]}, ensure_ascii=False))
        return 0
    except (LocalAPIError, OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, LocalAPIError) else 'Local API operation failed locally.')
        return 1
if __name__ == '__main__':
    raise SystemExit(main())
