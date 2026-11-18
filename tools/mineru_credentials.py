# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import os
from pathlib import Path
from library_platform import secret_suffix
from library_paths import DATA, SECRETS
DEFAULT_ROOT = DATA
DEFAULT_PATH = SECRETS / ('mineru_api' + secret_suffix())
MAX_TOKEN_LENGTH = 4096
MAX_FILE_BYTES = 64 * 1024

class CredentialError(RuntimeError):
    message = 'MinerU credential security error: credential configuration is unavailable.'

    def __init__(self):
        super().__init__(self.message)

class InvalidTokenError(CredentialError):
    message = 'MinerU credential security error: a token must contain 1-4096 visible ASCII characters without whitespace.'

class CredentialReadError(CredentialError):
    message = 'MinerU credential security error: saved credentials could not be read or decrypted; fallback to older credentials is disabled.'

class CredentialSaveError(CredentialError):
    message = 'MinerU credential security error: encrypted credentials could not be saved.'

def _dpapi(data, *, decrypt=False):
    if __package__:
        from .zotero_local import _dpapi as protect
    else:
        from zotero_local import _dpapi as protect
    return protect(data, decrypt=decrypt)

def _atomic_bytes(path, data):
    if __package__:
        from .zotero_local import _atomic_bytes as write
    else:
        from zotero_local import _atomic_bytes as write
    write(path, data)

def _file_lock(path):
    if __package__:
        from .glm_concurrency import FileLock
    else:
        from glm_concurrency import FileLock
    return FileLock(path.with_name(path.name + '.lock'))

def _path(path, root):
    try:
        if path is not None and root is not None:
            raise ValueError
        if path is not None:
            return Path(path)
        return DEFAULT_PATH if root is None else Path(root) / '.secrets' / ('mineru_api' + secret_suffix())
    except Exception:
        pass
    raise CredentialError()

def _normalize(value, *, allow_empty=False):
    if value is None and allow_empty:
        return None
    if type(value) is not str:
        raise InvalidTokenError()
    value = value.strip(' \t\r\n\x0b\x0c')
    if not value and allow_empty:
        return None
    if not 1 <= len(value) <= MAX_TOKEN_LENGTH or any((not 33 <= ord(c) <= 126 for c in value)):
        raise InvalidTokenError()
    return value

def _environment_token(environ):
    try:
        return _normalize((os.environ if environ is None else environ).get('MINERU_API_TOKEN'), allow_empty=True)
    except Exception:
        pass
    raise InvalidTokenError()

def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except Exception:
        pass
    raise CredentialReadError()

def _read(path):
    if not _present(path):
        return None
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) <= MAX_FILE_BYTES:
            return data
    except Exception:
        pass
    raise CredentialReadError()

def _select(path, root, crypto, environ):
    override = _environment_token(environ)
    if override is not None:
        return (override, 'environment')
    selected_path = _path(path, root)
    encrypted = _read(selected_path)
    if encrypted is not None:
        try:
            if not encrypted:
                raise ValueError
            plaintext = (_dpapi if crypto is None else crypto)(encrypted, decrypt=True)
            return (_normalize(plaintext.decode('ascii')), 'encrypted')
        except Exception:
            pass
        raise CredentialReadError()
    legacy = _read(selected_path.with_name('mineru_token'))
    if legacy is not None:
        try:
            value = _normalize(legacy.decode('ascii'), allow_empty=True)
            return (value, 'legacy_file' if value is not None else 'missing')
        except Exception:
            pass
        raise CredentialReadError()
    return (None, 'missing')

def load_token(*, path=None, root=None, crypto=None, environ=None):
    return _select(path, root, crypto, environ)[0]

def public_status(*, path=None, root=None, crypto=None, environ=None):
    value, source = _select(path, root, crypto, environ)
    saved = source == 'encrypted'
    if source == 'environment':
        saved = _present(_path(path, root))
    return {'configured': value is not None, 'saved': saved, 'source': source}

def save_token(value, *, path=None, root=None, crypto=None, atomic_write=None, environ=None):
    normalized = _normalize(value, allow_empty=True)
    if normalized is None:
        return public_status(path=path, root=root, crypto=crypto, environ=environ)
    override = _environment_token(environ)
    selected_path = _path(path, root)
    try:
        lock = _file_lock(selected_path)
        try:
            lock.acquire(timeout=30)
            plaintext = normalized.encode('ascii')
            ciphertext = (_dpapi if crypto is None else crypto)(plaintext)
            if type(ciphertext) is not bytes or not ciphertext or len(ciphertext) > MAX_FILE_BYTES or (ciphertext == plaintext):
                raise ValueError
            (_atomic_bytes if atomic_write is None else atomic_write)(selected_path, ciphertext)
        finally:
            lock.release()
    except Exception:
        pass
    else:
        return {'configured': True, 'saved': True, 'source': 'environment' if override is not None else 'encrypted'}
    raise CredentialSaveError()
