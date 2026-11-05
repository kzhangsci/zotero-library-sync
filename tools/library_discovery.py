# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.astimezone(dt.timezone.utc)

def utc(value):
    return timestamp(value).isoformat()

def revision(runs):
    path = Path(runs) / 'discovery.json'
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

def history(runs):
    states = []
    for path in sorted(Path(runs).glob('*/state.json')):
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            timestamp(state['started'])
            states.append(state)
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f'同步历史损坏，未扩大扫描范围：{path}') from exc
    return sorted(states, key=lambda s: timestamp(s['started']))

def apply_result(ledger, state):
    result = copy.deepcopy(ledger)
    papers = {p['key']: p for p in state.get('papers', [])}
    requested = state.get('requested', list(papers))
    for key in requested:
        if not re.fullmatch('[A-Z0-9]{8}', key):
            raise RuntimeError('历史条目 key 无效，未改动待办')
        old = result['entries'].get(key, {})
        if old.get('run_started') and timestamp(old['run_started']) > timestamp(state['started']):
            continue
        paper = papers.get(key, {})
        complete = state.get('status') == 'completed' or (state.get('status') == 'partial' and state.get('workflow_complete') is True)
        complete = complete and paper.get('qa', {}).get('ok') is True and bool(paper.get('index'))
        result['entries'][key] = {**old, 'status': 'completed' if complete else 'pending', 'citekey': paper.get('citekey', old.get('citekey', '')), 'run_started': state['started'], 'run_id': state['id'], 'reason': '' if complete else state.get('error', '尚未通过完整收尾')}
    return result

def load(runs, *, seed_since=None):
    path = Path(runs) / 'discovery.json'
    states = history(runs)
    if path.exists():
        try:
            ledger = json.loads(path.read_text(encoding='utf-8'))
            if ledger['schema'] != 1 or not isinstance(ledger['entries'], dict):
                raise ValueError('schema')
            timestamp(ledger['since'])
            for key, entry in ledger['entries'].items():
                if not re.fullmatch('[A-Z0-9]{8}', key) or entry.get('status') not in ('pending', 'completed'):
                    raise ValueError('entry')
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('新增发现台账损坏；请检查 discovery.json，不能退回扫描历史库') from exc
    else:
        completed = [s for s in states if s.get('status') == 'completed']
        if completed:
            since = utc(completed[0]['started'])
        elif seed_since:
            dt.date.fromisoformat(seed_since)
            since = utc(seed_since)
        else:
            raise RuntimeError('没有可靠的新增起点；首次请用 -Since YYYY-MM-DD 预览并确认，绝不自动处理历史积压')
        ledger = {'schema': 1, 'since': since, 'entries': {}}
    for state in states:
        ledger = apply_result(ledger, state)
    return ledger

def discover(client, runs, limit=0):
    from library_paths import BASELINE_DATE
    ledger = load(runs, seed_since=BASELINE_DATE)
    items = client.list_all('items/top')
    candidates = {}
    for item in items:
        d, key = (item['data'], item['key'])
        if d.get('deleted') or d.get('itemType') in ('attachment', 'annotation', 'note'):
            continue
        previous = ledger['entries'].get(key, {})
        if previous.get('status') == 'completed':
            continue
        try:
            recent = timestamp(d['dateAdded']) >= timestamp(ledger['since'])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f'{key} 的添加日期无效，不能可靠判断是否新增') from exc
        if recent or previous.get('status') == 'pending':
            candidates[key] = {'key': key, 'citekey': d.get('citationKey', ''), 'title': d.get('title', ''), 'dateAdded': d['dateAdded']}
    ordered = sorted(candidates.values(), key=lambda p: (ledger['entries'].get(p['key'], {}).get('last_reviewed', ''), timestamp(p['dateAdded']), p['key']))
    cap = min(limit or 25, 25)
    selected, deferred = (ordered[:cap], [])
    for p in ordered[cap:]:
        deferred.append({**p, 'blocked': ['超出本批上限；保留到下次确认']})
    for key, entry in ledger['entries'].items():
        if entry['status'] == 'pending' and key not in candidates:
            deferred.append({'key': key, 'citekey': entry.get('citekey', ''), 'blocked': ['条目不在当前有效库中；保留待办，请人工核对']})
    return {'mode': 'automatic', 'since': ledger['since'], 'seed_since': BASELINE_DATE, 'revision': revision(runs), 'observed': ordered, 'deferred': deferred, 'selected': [p['key'] for p in selected]}

def begin(runs, plan):
    discovery = plan['discovery']
    if discovery['revision'] != revision(runs):
        raise RuntimeError('预览后新增发现台账已变化，请重新预览；没有重复启动')
    ledger = load(runs, seed_since=discovery.get('seed_since'))
    if utc(ledger['since']) != utc(discovery['since']):
        raise RuntimeError('新增发现起点变化，请重新预览')
    for p in discovery['observed']:
        old = ledger['entries'].get(p['key'], {})
        ledger['entries'][p['key']] = {**old, 'citekey': p.get('citekey', ''), 'status': old.get('status', 'pending')}
    for p in plan.get('deferred', []):
        old = ledger['entries'].get(p['key'], {})
        ledger['entries'][p['key']] = {**old, 'citekey': p.get('citekey', ''), 'status': 'pending', 'reason': '; '.join(p.get('blocked', []))}
    for p in plan['items']:
        old = ledger['entries'].get(p['key'], {})
        ledger['entries'][p['key']] = {**old, 'status': 'pending', 'reason': '用户已确认，等待完整收尾'}
    for key in discovery.get('selected', [p['key'] for p in plan['items']]):
        if key in ledger['entries']:
            ledger['entries'][key]['last_reviewed'] = utc(plan['created'])
    return ledger
