# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import calendar
import datetime as dt
import json
from pathlib import Path
import re
REPORT_NAME = re.compile('(?:library_sync_\\d{8}_\\d{6}_[0-9a-f]{6}|sync_\\d{8}_\\d{6})\\.md')
PROTECTED_STATES = {'running', 'paused', 'pause_requested', 'starting'}

def _recovery_evidence_reports(runs):
    protected = set()
    for state_path in Path(runs).glob('*/state.json'):
        run_dir = state_path.parent
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
            report = state.get('report')
        except (OSError, ValueError, AttributeError):
            continue
        if not report:
            continue
        if (run_dir / 'pause_request.json').is_file() and state.get('status') != 'completed' or (state.get('status') == 'paused' and (run_dir / 'pause_receipt.json').is_file()):
            protected.add(Path(report).resolve())
        elif state.get('status') in ('partial',) and state.get('needs_action'):
            protected.add(Path(report).resolve())
    return protected

def _ledger_referenced_reports(runs):
    path = Path(runs) / 'issues.json'
    if not path.is_file():
        return set()
    try:
        ledger = json.loads(path.read_text(encoding='utf-8'))
        return {Path(row['run_report']).resolve() for row in ledger.get('items', {}).values() if isinstance(row, dict) and row.get('run_report') and (row.get('status') not in ('complete', 'excluded'))}
    except (OSError, ValueError):
        return set()

def expire_reports(reports, runs, *, now=None, apply=False):
    reports, runs = (Path(reports), Path(runs))
    now = now or dt.datetime.now().astimezone()
    month = now.year * 12 + now.month - 1 - 3
    year, month = divmod(month, 12)
    month += 1
    cutoff = now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))
    protected = set()
    result = {'cutoff': cutoff.isoformat(), 'eligible': [], 'deleted': [], 'errors': []}
    try:
        for state_path in runs.glob('*/state.json'):
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if state.get('status') in PROTECTED_STATES and state.get('report'):
                protected.add(Path(state['report']).resolve())
        protected |= _recovery_evidence_reports(runs) | _ledger_referenced_reports(runs)
    except (OSError, ValueError, TypeError, AttributeError):
        result['errors'].append('Cannot verify active report ownership')
        return result
    if not reports.is_dir():
        return result
    root = reports.resolve()
    for path in reports.glob('*.md'):
        if not REPORT_NAME.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            resolved = path.resolve()
            if resolved.parent != root or resolved in protected:
                continue
            stamp = re.search('(\\d{8}_\\d{6})', path.name).group(1)
            created = dt.datetime.strptime(stamp, '%Y%m%d_%H%M%S').replace(tzinfo=now.tzinfo)
            before = path.stat()
            if created >= cutoff or before.st_mtime >= cutoff.timestamp():
                continue
            result['eligible'].append(path.name)
            if apply and path.stat() == before and (not path.is_symlink()):
                path.unlink()
                result['deleted'].append(path.name)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            result['errors'].append(path.name)
    return result
