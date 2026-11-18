# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import errno
import hashlib
import inspect
import json
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from library_paths import DATA
ROOT = DATA / '.zcode_work' / 'concurrency'
CACHE_VERSION = 1

def positive_int(value, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be a positive integer') from exc
    if result < 1:
        raise ValueError(f'{name} must be a positive integer')
    return result

class FileLock:

    def __init__(self, path: Path):
        self.path, self.file = (Path(path), None)

    def acquire(self, timeout: float | None=None) -> 'FileLock':
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        start = time.monotonic()
        try:
            while True:
                try:
                    self.file.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if timeout is not None and time.monotonic() - start >= timeout:
                        raise TimeoutError(f'lock busy: {self.path.name}') from None
                    time.sleep(0.05)
        except BaseException:
            self.file.close()
            self.file = None
            raise

    def release(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()

def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def global_limit() -> int:
    return positive_int(os.environ.get('GLM_MAX_CONCURRENCY') or 2, 'GLM_MAX_CONCURRENCY')

@contextmanager
def glm_slot(*, root: Path | None=None, limit: int | None=None, timeout: float | None=None):
    root = ROOT if root is None else Path(root)
    limit = global_limit() if limit is None else positive_int(limit, 'limit')
    started, held = (time.monotonic(), None)
    while held is None:
        remaining = None if timeout is None else max(0, timeout - (time.monotonic() - started))
        with_lock = FileLock(root / 'control.lock').acquire(remaining)
        try:
            config = root / 'limit.json'
            configured = json.loads(config.read_text(encoding='utf-8'))['limit'] if config.exists() else limit
            if configured != limit:
                idle = []
                try:
                    for i in range(max(configured, limit)):
                        idle.append(FileLock(root / f'slot_{i}.lock').acquire(0))
                except TimeoutError:
                    raise RuntimeError(f'GLM concurrency limit mismatch: active={configured}, requested={limit}; restart workers with one limit') from None
                finally:
                    for lock in idle:
                        lock.release()
            if not config.exists() or configured != limit:
                atomic_json(config, {'limit': limit})
            for i in range(limit):
                try:
                    held = FileLock(root / f'slot_{i}.lock').acquire(0)
                    break
                except TimeoutError:
                    continue
        finally:
            with_lock.release()
        if held is None:
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError('waiting for a GLM concurrency slot')
            time.sleep(0.05)
    try:
        yield {'slot': i, 'wait_seconds': time.monotonic() - started, 'limit': limit}
    finally:
        held.release()

class _WindowsJob:

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c = c
        self.k = c.WinDLL('kernel32', use_last_error=True)

        class Basic(c.Structure):
            _fields_ = [('ProcessTime', c.c_int64), ('JobTime', c.c_int64), ('Flags', w.DWORD), ('MinWS', c.c_size_t), ('MaxWS', c.c_size_t), ('Active', w.DWORD), ('Affinity', c.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]

        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]

        class Extended(c.Structure):
            _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMemory', c.c_size_t), ('JobMemory', c.c_size_t), ('PeakProcess', c.c_size_t), ('PeakJob', c.c_size_t)]
        self.k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.k.CreateJobObjectW.restype = w.HANDLE
        self.k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        settings = Extended()
        settings.Basic.Flags = 8192
        if not self.k.SetInformationJobObject(self.handle, 9, c.byref(settings), c.sizeof(settings)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def attach_and_resume(self, process):
        if not self.k.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.c.WinError(self.c.get_last_error())
        native = self.c.WinDLL('ntdll')
        native.NtResumeProcess.argtypes = [self.c.c_void_p]
        native.NtResumeProcess.restype = self.c.c_long
        status = native.NtResumeProcess(int(process._handle))
        if status != 0:
            raise OSError(f'NtResumeProcess failed: {status:#x}')

    def close(self):
        if self.handle:
            self.k.TerminateJobObject(self.handle, 1)
            self.k.CloseHandle(self.handle)
            self.handle = None

def run_cli(cmd, *, timeout, env):
    job = _WindowsJob() if os.name == 'nt' else None
    process = None
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', env=env, shell=False, creationflags=134217732 if job else 0)
        if job:
            job.attach_and_resume(process)
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    finally:
        if job:
            job.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()

def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()

def qc_reply(prompt, backend, cache_dir, validate):
    identity = backend.cache_identity
    key = _digest(['qc-reply-v1', cache_dir.name, identity, prompt])
    path = cache_dir / (key + '.json')
    with FileLock(path.with_suffix('.lock')):
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
            reply, model = (entry['reply'], entry['model'])
            if entry['key'] == key and entry['sha256'] == _digest([key, model, reply]) and isinstance(reply, str) and isinstance(model, str) and model.startswith(identity['provider'] + '/'):
                validate(reply)
                backend._mark(model)
                return reply
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass
        reply = backend.ask(prompt)
        validate(reply)
        model = backend.last_response_model
        if model:
            try:
                atomic_json(path, {'key': key, 'model': model, 'reply': reply, 'sha256': _digest([key, model, reply])})
            except OSError:
                if backend.log:
                    backend.log('质检分块缓存写入失败，本次继续校验完整回复。')
        return reply

def translate_cached(paras, glossary, backend, translator, cache_dir: Path | None):
    model = getattr(backend, 'model', None)
    api_identity = getattr(backend, 'cache_identity', None)
    if cache_dir is None or not model or (not api_identity and (not model.startswith('bigmodel/'))):
        return translator(paras, glossary, backend)
    from glm_client import REASONING_LEVEL
    guard = translator.__globals__.get('STALL_RE')
    kana = translator.__globals__.get('KANA')
    identity = {'v': CACHE_VERSION, 'paras': paras, 'glossary': glossary, 'model': model, 'reasoning': REASONING_LEVEL, 'translator': inspect.getsource(translator), 'stall_guard': (guard.pattern, guard.flags) if guard else None, 'language_detector': (kana.pattern, kana.flags) if kana else None}
    if api_identity is not None:
        identity['api'] = api_identity
        identity['reasoning'] = api_identity.get('reasoning_effort', 'default')
    key = _digest(identity)
    path = Path(cache_dir) / f'{key}.json'
    with FileLock(path.with_suffix('.lock')):
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
            result = entry['result']
            source_models = entry.get('source_models', []) if api_identity else [model]
            provenance_ok = not api_identity or (isinstance(source_models, list) and bool(source_models) and all((isinstance(m, str) and re.fullmatch(re.escape(api_identity['provider']) + '/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', m) for m in source_models)))
            if entry['key'] == key and entry['sha256'] == _digest(result) and isinstance(result, list) and (len(result) == len(paras)) and all((isinstance(p, str) and p.strip() for p in result)) and provenance_ok:
                for source_model in source_models:
                    backend._mark(source_model)
                return result
        except (OSError, ValueError, KeyError, TypeError):
            pass

        class AttemptRecorder:

            def __init__(self):
                self.errors = 0
                self.successes = 0
                self.log = backend.log
                self.source_models = set()

            def ask(self, prompt):
                try:
                    result = backend.ask(prompt)
                except Exception:
                    self.errors += 1
                    raise
                self.successes += 1
                actual = getattr(backend, 'last_response_model', None)
                if actual:
                    self.source_models.add(actual)
                return result
        recorder = AttemptRecorder()
        result = translator(paras, glossary, recorder)
        if recorder.successes and recorder.errors < 3 and (backend.model == model) and (len(result) == len(paras)) and all((isinstance(p, str) and p.strip() for p in result)) and (not any(('此段模型拒绝翻译' in p for p in result))):
            if not api_identity or recorder.source_models:
                atomic_json(path, {'key': key, 'result': result, 'sha256': _digest(result), 'source_models': sorted(recorder.source_models)})
        return result

class RequestBudgetExceeded(RuntimeError):
    pass

class RequestBudget:

    def __init__(self, maximum: int):
        self.maximum = positive_int(maximum, 'maximum')
        self.used = 0
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            if self.used >= self.maximum:
                raise RequestBudgetExceeded(f'benchmark request budget exhausted ({self.maximum})')
            self.used += 1
