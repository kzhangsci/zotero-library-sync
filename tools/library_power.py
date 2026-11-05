# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import argparse
import contextlib
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time
import library_progress as progress
ES_CONTINUOUS = 2147483648
ES_SYSTEM_REQUIRED = 1

def execution_state(flags):
    if os.name != 'nt':
        raise RuntimeError('Temporary wake protection requires Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetThreadExecutionState.argtypes = (wintypes.DWORD,)
    kernel.SetThreadExecutionState.restype = wintypes.DWORD
    if not kernel.SetThreadExecutionState(flags):
        raise OSError('SetThreadExecutionState failed; wake protection is not active')

@contextlib.contextmanager
def system_awake():
    execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        execution_state(ES_CONTINUOUS)

def same_owner(first, second):
    return bool(second and all((first.get(k) == second.get(k) for k in ('id', 'pid', 'created', 'exe'))))

def follow_owner(runs, owner, *, poll_seconds=5, read_active=None, wait=None):
    read_active = read_active or progress.active_run
    wait = wait or time.sleep
    while same_owner(owner, read_active(runs)):
        wait(poll_seconds)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int, choices=[1], default=1)
    args = parser.parse_args(argv)
    from library_sync import RUNS, save_json, workflow_lock
    progress._run_id(args.run_id)
    owner = progress.active_run(RUNS)
    if owner is None or owner['id'] != args.run_id:
        print('No matching live controller; no wake request and no sync started.', flush=True)
        return 1
    if not args.apply:
        print(f'dry-run: temporary system wake guard for {args.run_id}; no power call', flush=True)
        return 0
    run = Path(RUNS) / args.run_id
    with workflow_lock(run / 'power_guard.lock'):
        if not same_owner(owner, progress.active_run(RUNS)):
            return 1
        record = {'run_id': args.run_id, 'owner': owner, 'helper': progress.process_identity(os.getpid()), 'active': False}
        try:
            with system_awake():
                record.update(active=True, started=time.time())
                save_json(run / 'power_guard.json', record)
                print('Temporary system wake request active; screen/manual sleep unchanged.', flush=True)
                follow_owner(RUNS, owner)
        finally:
            record.update(active=False, ended=time.time())
            save_json(run / 'power_guard.json', record)
        print('Owner finished/exited/changed; temporary wake request released.', flush=True)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
