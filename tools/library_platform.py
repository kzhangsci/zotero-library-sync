# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import contextlib
import ctypes
import hashlib
import json
import os
import posixpath
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
IS_MAC = sys.platform == 'darwin'
KEYCHAIN_SERVICE = b'org.library-sync.credentials'
REFERENCE_PREFIX = b'LibrarySync-Keychain-v1\n'

def secret_suffix():
    return '.keychain' if IS_MAC else '.dpapi'

def mac_identity(pid, *, api=None):
    if api is None:
        import psutil as api
    try:
        process = api.Process(pid)
        born = process.create_time()
        executable = process.exe()
        if not process.is_running() or process.create_time() != born:
            return None
        if not posixpath.isabs(executable):
            raise RuntimeError('Process identity unavailable')
        return {'pid': pid, 'created': format(born, '.9f'), 'exe': executable}
    except (api.NoSuchProcess, api.ZombieProcess):
        return None

def mac_workers(*, api=None):
    if api is None:
        import psutil as api
    result = []
    for process in api.process_iter():
        try:
            if not process.name().lower().startswith('python'):
                continue
            if process.uids().real != os.getuid():
                continue
            args = process.cmdline()
            if any((Path(arg).name in {'sync.py', 'sync_runner.py', 'intake_new.py'} for arg in args)):
                result.append({'ProcessId': process.pid, 'CommandLine': ' '.join(args)})
        except (api.NoSuchProcess, api.ZombieProcess):
            continue
        except api.AccessDenied:
            raise RuntimeError('无法核验同步进程；请检查系统进程访问权限') from None
    return result

@contextlib.contextmanager
def posix_lock(path):
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('另一条一键同步仍在运行；没有重复启动') from None
        try:
            yield stream
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

@contextlib.contextmanager
def posix_guard(path, *, writable=False, create=False, delete_on_close=False):
    import fcntl
    path = Path(path)
    flags = (os.O_RDWR if writable else os.O_RDONLY) | (os.O_CREAT | os.O_EXCL if create else 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 384)
    with os.fdopen(fd, 'r+b' if writable else 'rb') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = os.fstat(stream.fileno())
        try:
            yield stream
        finally:
            if delete_on_close and path.exists():
                current = path.stat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink()

def reclaim_mac_step_lock(path):
    import library_progress as progress
    path = Path(path)
    if path.name not in {'.sync_md.lock', '.sync_qc.lock', '.sync_zh.lock'} or path.resolve() != path.absolute():
        return False
    try:
        with posix_guard(path) as stream:
            identity = os.fstat(stream.fileno())
            if identity.st_uid != os.getuid() or identity.st_size > 2048:
                return False
            try:
                record = json.loads(stream.read(2049))
            except (ValueError, UnicodeError):
                return False
            if not isinstance(record, dict) or record.get('schema') != 1 or record.get('kind') != 'library-sync-step-lock':
                return False
            if progress.owner_matches(record.get('owner')):
                return False
            current = path.lstat()
            if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                return False
            path.unlink()
            return True
    except FileNotFoundError:
        return True
    except (OSError, progress.ProgressError):
        return False

def mac_move_locked(stream, source, destination):
    if not IS_MAC:
        raise RuntimeError('Native macOS required')
    source, destination = (Path(source), Path(destination))
    held = os.fstat(stream.fileno())
    expected = (held.st_dev, held.st_ino)
    current = source.lstat()
    if (current.st_dev, current.st_ino) != expected:
        raise RuntimeError('归档前文件已被替换；未移动')
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int

    def move(first, second):
        if rename(os.fsencode(first), os.fsencode(second), 4):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(first))
    move(source, destination)
    moved = destination.lstat()
    if (moved.st_dev, moved.st_ino) != expected:
        try:
            move(destination, source)
        except OSError:
            raise RuntimeError('归档时文件并发变化；已保留归档副本，请检查：' + str(destination)) from None
        raise RuntimeError('归档时文件并发变化；已恢复，未继续处理')

def keychain_protect(data, *, decrypt=False, backend=None):
    backend = backend or NativeKeychain()
    if decrypt:
        if not data.startswith(REFERENCE_PREFIX):
            raise RuntimeError('此凭据不属于本机 Keychain；请在当前电脑重新输入')
        account = data[len(REFERENCE_PREFIX):].decode('ascii')
        if str(uuid.UUID(account)) != account:
            raise ValueError('Invalid Keychain reference')
        return backend.get(account)
    if not isinstance(data, bytes) or len(data) > 1024 * 1024:
        raise ValueError('Credential payload rejected')
    account = str(uuid.uuid4())
    backend.add(account, data)
    return REFERENCE_PREFIX + account.encode('ascii')

class NativeKeychain:

    def __init__(self):
        if not IS_MAC:
            raise RuntimeError('macOS Keychain unavailable')
        self.security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        ptr, size, status = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32)
        self.security.SecKeychainAddGenericPassword.argtypes = [ptr, size, ptr, size, ptr, size, ptr, ctypes.POINTER(ptr)]
        self.security.SecKeychainAddGenericPassword.restype = status
        self.security.SecKeychainFindGenericPassword.argtypes = [ptr, size, ptr, size, ptr, ctypes.POINTER(size), ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
        self.security.SecKeychainFindGenericPassword.restype = status
        self.security.SecKeychainItemFreeContent.argtypes = [ptr, ptr]
        self.security.SecKeychainItemFreeContent.restype = status
        self.security.SecKeychainItemDelete.argtypes = [ptr]
        self.security.SecKeychainItemDelete.restype = status
        self.cf.CFRelease.argtypes = [ptr]
        self.cf.CFRelease.restype = None

    def add(self, account, data):
        account = account.encode('ascii')
        status = self.security.SecKeychainAddGenericPassword(None, len(KEYCHAIN_SERVICE), KEYCHAIN_SERVICE, len(account), account, len(data), data, None)
        if status:
            raise RuntimeError(f'Keychain 保存失败（OSStatus {status}）；未使用明文保存')

    def get(self, account):
        account = account.encode('ascii')
        length, content, item = (ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p())
        status = self.security.SecKeychainFindGenericPassword(None, len(KEYCHAIN_SERVICE), KEYCHAIN_SERVICE, len(account), account, ctypes.byref(length), ctypes.byref(content), ctypes.byref(item))
        if status:
            raise RuntimeError(f'Keychain 读取失败（OSStatus {status}）；请检查本机授权')
        try:
            if length.value > 1024 * 1024:
                raise ValueError('Credential payload too large')
            return ctypes.string_at(content, length.value)
        finally:
            self.security.SecKeychainItemFreeContent(None, content)
            if item:
                self.cf.CFRelease(item)

    def delete(self, account):
        account = account.encode('ascii')
        item = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(None, len(KEYCHAIN_SERVICE), KEYCHAIN_SERVICE, len(account), account, None, None, ctypes.byref(item))
        if status == -25300:
            return
        if status:
            raise RuntimeError('Keychain cleanup unavailable')
        try:
            if self.security.SecKeychainItemDelete(item):
                raise RuntimeError('Keychain cleanup unavailable')
        finally:
            if item:
                self.cf.CFRelease(item)

def discard_keychain_reference(data):
    if IS_MAC and data and data.startswith(REFERENCE_PREFIX):
        account = data[len(REFERENCE_PREFIX):].decode('ascii')
        if str(uuid.UUID(account)) != account:
            raise ValueError('Invalid Keychain reference')
        NativeKeychain().delete(account)

def posix_commit(repo, paths, message, baseline_dirty=(), *, expected_hashes=None, guard=None):
    import library_sync as s
    repo = Path(repo).resolve()
    if s.git(repo, 'diff', '--cached', '--name-only', '-z'):
        raise RuntimeError('Git index contains pre-existing staged work; commit stopped without changing it')
    allowed = sorted(set(paths) & s.dirty_paths(repo) - set(baseline_dirty))
    if not allowed:
        return None
    gitdir = Path(s.git(repo, 'rev-parse', '--absolute-git-dir').strip())
    lock = gitdir / 'index.lock'
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
    except FileExistsError:
        raise RuntimeError('Git index 正在使用；未修改提交或暂存区') from None
    with tempfile.TemporaryDirectory(prefix='library-sync-index-', dir=gitdir) as temporary:
        index = Path(temporary) / 'index'
        env = {**os.environ, 'GIT_INDEX_FILE': str(index)}

        def git(*args, data=None):
            result = subprocess.run([str(s.GIT or 'git'), '-C', str(repo), *args], input=data, capture_output=True, env=env, timeout=30)
            if result.returncode:
                raise RuntimeError('Git snapshot operation failed; private index preserved no user staging')
            return result.stdout.decode('utf-8').strip()
        changed_ref = False
        parent = None
        try:
            if s.git(repo, 'diff', '--cached', '--name-only', '-z'):
                raise RuntimeError('Git staging changed concurrently; automatic commit stopped')
            head = subprocess.run([str(s.GIT or 'git'), '-C', str(repo), 'rev-parse', '--verify', 'HEAD'], capture_output=True)
            parent = head.stdout.decode().strip() if head.returncode == 0 else None
            git('read-tree', parent) if parent else git('read-tree', '--empty')
            for name in allowed:
                path = repo / name
                if not path.is_file() or path.is_symlink() or (not path.resolve().is_relative_to(repo)):
                    raise RuntimeError('Refusing unsafe commit path')
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if expected_hashes is not None and expected_hashes.get(str(path)) != digest:
                    raise RuntimeError('Commit input changed concurrently')
                blob = git('hash-object', '-w', '--stdin', data=content)
                mode = '100755' if path.stat().st_mode & 73 else '100644'
                git('update-index', '--add', '--cacheinfo', mode, blob, name)
            if guard:
                guard()
            tree = git('write-tree')
            commit = git('commit-tree', tree, *(['-p', parent] if parent else []), data=(message + '\n').encode())
            with os.fdopen(lock_fd, 'wb') as output:
                lock_fd = None
                output.write(index.read_bytes())
                output.flush()
                os.fsync(output.fileno())
            git('update-ref', '-m', message, 'HEAD', commit, parent or '0' * 40)
            changed_ref = True
            os.replace(lock, gitdir / 'index')
            return commit[:12]
        except Exception:
            if changed_ref:
                if parent:
                    git('update-ref', 'HEAD', parent, commit)
                else:
                    git('update-ref', '-d', 'HEAD', commit)
            raise
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            lock.unlink(missing_ok=True)
