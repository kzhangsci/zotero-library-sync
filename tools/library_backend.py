# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from pathlib import Path
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time

def fingerprint(root):
    names = ('library_plugin_bridge', 'library_backend', 'library_remedy', 'library_control', 'library_sync', 'library_progress', 'library_issues', 'model_api', 'sync')
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((Path(root) / 'tools' / (name + '.py')).read_bytes())
    return digest.hexdigest()

class Identity:

    def __init__(self, root):
        self.root = Path(root)
        self.version = json.loads((self.root / 'tools/zotero_library_sync/manifest.json').read_text(encoding='utf-8'))['version']
        self.build = fingerprint(self.root)
        self.started = dt.datetime.now().isoformat(timespec='seconds')

    def public(self):
        try:
            changed = fingerprint(self.root) != self.build
        except OSError:
            changed = True
        return dict(version=self.version, build=self.build, pid=os.getpid(), started=self.started, disk_changed=changed, protocol=1)

def _command(pid):
    if os.name == 'nt':
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}" -ErrorAction Stop).CommandLine'], capture_output=True, encoding='utf-8', timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('无法核验后台命令行；未重启')
        command = result.stdout.strip()
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        shell.CommandLineToArgvW.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
        shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        argc = ctypes.c_int()
        argv = shell.CommandLineToArgvW(command, ctypes.byref(argc))
        if not argv:
            raise RuntimeError('无法核验后台参数；未重启')
        try:
            return list(argv[:argc.value])
        finally:
            kernel = ctypes.WinDLL('kernel32')
            kernel.LocalFree.argtypes = (ctypes.c_void_p,)
            kernel.LocalFree(argv)
    import psutil
    process = psutil.Process(pid)
    if process.uids().real != os.getuid():
        raise RuntimeError('后台不属于当前用户')
    return process.cmdline()

@contextlib.contextmanager
def verified_process(pid, executable, script):
    import library_progress as progress
    owner = progress.process_identity(pid)
    if not owner or not image_matches(owner, executable):
        raise RuntimeError('后台解释器身份不匹配；未重启')
    args = _command(pid)
    if len(args) != 4 or args[2:] != ['--serve', '--apply'] or os.path.normcase(os.path.realpath(args[1])) != os.path.normcase(os.path.realpath(script)):
        raise RuntimeError('后台命令行身份不匹配；未重启')
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(4096 | 1048576 | 1, False, pid)
        if not handle:
            raise RuntimeError('无法打开指定后台进程；未重启')
        try:
            if not progress.owner_matches(owner):
                raise RuntimeError('后台进程身份已变化')

            def stop():
                if not kernel.TerminateProcess(handle, 0):
                    raise RuntimeError('后台退出失败')
                if kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise RuntimeError('等待后台退出超时')
            yield stop
        finally:
            kernel.CloseHandle(handle)
    else:
        import psutil
        process = psutil.Process(pid)
        if not progress.owner_matches(owner):
            raise RuntimeError('后台进程身份已变化')

        def stop():
            process.terminate()
            process.wait(timeout=5)
        yield stop

def image_matches(owner, executable):
    expected = executable
    if sys.platform == 'darwin':
        import library_progress as progress
        current = progress.process_identity(os.getpid())
        if not current:
            return False
        expected = current['exe']
    return os.path.normcase(os.path.realpath(owner['exe'])) == os.path.normcase(os.path.realpath(expected))

def restart_idle(expected_pid):
    import requests
    import library_sync as workflow
    from glm_concurrency import FileLock
    from library_plugin_bridge import MANIFEST
    expected_pid = int(expected_pid)
    if expected_pid <= 0 or expected_pid == os.getpid():
        raise ValueError('Invalid bridge PID')
    script = Path(__file__).with_name('library_plugin_bridge.py').resolve()
    before = json.loads(MANIFEST.read_text(encoding='utf-8'))
    if before.get('pid') != expected_pid or before.get('host') != '127.0.0.1':
        raise RuntimeError('连接描述符已变化；未重启')
    session = requests.Session()
    session.trust_env = False
    address = 'http://127.0.0.1:' + str(int(before['port']))
    headers = {'X-Library-Sync-Token': before['token']}

    def status():
        response = session.get(address + '/v1/status', headers=headers, timeout=5, allow_redirects=False)
        response.raise_for_status()
        value = response.json()
        if value.get('active') is not False:
            raise RuntimeError('任务正在运行；未重启后台')
        return value
    with FileLock(workflow.RUNS / 'bridge_restart.lock'), workflow.launch_lock(), workflow.workflow_lock():
        workflow.assert_idle()
        value = status()
        if json.loads(MANIFEST.read_text(encoding='utf-8')) != before:
            raise RuntimeError('后台连接已被替换；未重启')
        with verified_process(expected_pid, sys.executable, script) as stop:
            if 'backend-restart-v1' in value.get('capabilities', []):
                response = session.post(address + '/v1/service/stop', json={'confirm': True}, headers=headers, timeout=5, allow_redirects=False)
                response.raise_for_status()
                if response.json().get('stopping') is not True:
                    raise RuntimeError('后台未确认退出')
                deadline = time.monotonic() + 8
                import library_progress as progress
                owner = progress.process_identity(expected_pid)
                while owner and progress.owner_matches(owner) and (time.monotonic() < deadline):
                    time.sleep(0.1)
                if owner and progress.owner_matches(owner):
                    raise RuntimeError('后台尚未退出；未重复启动')
            else:
                status()
                stop()
    subprocess.Popen([sys.executable, str(script), '--serve', '--apply'], cwd=workflow.REPO, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return 0
