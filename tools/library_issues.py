# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import datetime as dt
import json
import os
import re
from pathlib import Path
ISSUES_SCHEMA = 1
STATUSES = ('new', 'incomplete', 'needs_repair', 'missing_pdf', 'complete', 'excluded')
CATEGORIES = {'valid_existing': '已有产物可复核/可采纳', 'legacy_format': '旧版元数据/格式需迁移', 'translation': '译文对照/内容问题', 'english_qc': '原文/质检/解析问题', 'missing_pdf': '缺少正本 PDF 或路径失效', 'identity': '身份/重复/来源冲突', 'api': 'API/鉴权/超时/限流', 'user_modified': '用户已修改的产物', 'pending_paused': '等待/已暂停', 'other': '其他'}
QA_CODE_CATEGORIES = {'quote_coverage': 'translation', 'quote_source': 'translation', 'translation_marker': 'translation', 'translation_empty': 'translation', 'translation_placeholder': 'translation', 'translation_orphan': 'translation', 'translation_unchanged': 'translation', 'translation_short': 'translation', 'structure_coverage': 'translation', 'references_changed': 'translation', 'formula_mismatch': 'translation', 'image_coverage': 'translation', 'zh_preamble': 'translation', 'quote_format': 'translation', 'metadata_missing': 'legacy_format', 'metadata_citekey': 'legacy_format', 'metadata_zotero_key': 'legacy_format', 'metadata_zotero_link': 'legacy_format', 'metadata_mismatch': 'legacy_format', 'metadata_source': 'legacy_format', 'source_pdf': 'legacy_format', 'front_matter': 'legacy_format', 'qc_mismatch': 'legacy_format', 'filename_key': 'legacy_format', 'folder_missing': 'english_qc', 'fulltext_count': 'english_qc', 'note_missing': 'english_qc', 'qc_missing': 'english_qc', 'body_empty': 'english_qc', 'encoding': 'english_qc', 'read_error': 'english_qc', 'translation_missing': 'english_qc', 'image_path': 'english_qc', 'image_missing': 'english_qc', 'image_syntax': 'english_qc', 'image_empty': 'english_qc', 'formula_unbalanced': 'english_qc', 'primary_pdf': 'identity', 'primary_pdf_missing': 'missing_pdf', 'expected_key': 'identity', 'folder_key': 'identity', 'active_lock': 'pending_paused', 'input_changed': 'other'}
IDENTITY_PATTERNS = ('ambiguous attachment role', 'duplicate primary', 'duplicate primary/annotated', 'needs rekey', 'citation key collides', 'pinned citation key', 'identity changed', '正本路径', '附件记录或角色已变化', '重复 citekey')
USER_MODIFIED_PATTERNS = ('未质检原文有用户修改', '无法核实为本流水线上次产物', 'user edit', 'Existing Note changed')
API_PATTERNS = (re.compile('finish_reason=(length|content_filter|aborted|missing|unknown)', re.I), re.compile('(?i)\\b(timeout|timed out|429|rate.?limit|401|403|unauthorized|quota\\b)'))
MISSING_PDF_PATTERNS = ('no pdf;', 'missing attachment file', 'missing primary pdf', '没有 publicationTitle')

def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result

def issues_path(runs):
    return Path(runs) / 'issues.json'

def empty():
    return {'schema': ISSUES_SCHEMA, 'items': {}}

def load(runs):
    path = issues_path(runs)
    if not path.is_file():
        return empty()
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique)
    except (OSError, ValueError) as error:
        raise RuntimeError(f'问题处置台账损坏（{issues_path(runs).name}）：{error}；未改动，请人工检查') from error
    if not isinstance(value, dict) or value.get('schema') != ISSUES_SCHEMA or (not isinstance(value.get('items'), dict)):
        raise RuntimeError('问题处置台账结构无效；未改动，请人工检查 ' + str(path))
    for key, row in value['items'].items():
        if not re.fullmatch('[A-Z0-9]{8}', key or '') or not isinstance(row, dict) or row.get('status') not in STATUSES:
            raise RuntimeError(f'问题处置台账条目无效：{key}；未改动，请人工检查')
    return value

def save(runs, ledger):
    if ledger.get('schema') != ISSUES_SCHEMA:
        raise RuntimeError('问题处置台账 schema 无效')
    path = issues_path(runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)
    return ledger

def classify_qa(qa):
    categories, evidence = ([], {})
    for issue in (qa or {}).get('errors', []) if isinstance(qa, dict) else []:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get('code', ''))
        category = QA_CODE_CATEGORIES.get(code, 'other')
        evidence.setdefault(category, []).append({'code': code, 'message': str(issue.get('message', ''))[:300], **({'file': str(issue['file'])} if issue.get('file') else {}), **({'unit': issue['unit']} if issue.get('unit') is not None else {})})
    for category in sorted(evidence):
        categories.append({'category': category, 'label': CATEGORIES[category], 'evidence': evidence[category]})
    return categories

def classify_log(lines):
    hits = []
    for line in lines or []:
        if not isinstance(line, str):
            continue
        if any((pattern.search(line) for pattern in API_PATTERNS)) or re.search('(?i)(qc|translation) failed .*incomplete_completion', line):
            hits.append(line[:300])
    return [{'category': 'api', 'label': CATEGORIES['api'], 'evidence': [{'code': 'api_failure', 'message': line} for line in hits[:8]]}] if hits else []

def classify_reasons(reasons, library=None, citekey=''):
    categories = {}

    def add(category, message):
        categories.setdefault(category, []).append({'code': 'blocked', 'message': message[:300]})
    for reason in reasons or []:
        text = str(reason)
        lowered = text.lower()
        if any((pattern in lowered for pattern in MISSING_PDF_PATTERNS)) or 'no pdf' in lowered:
            absent = True
            match = re.search('missing attachment file: (.+)$', text)
            if match and library:
                absent = not Path(match.group(1).strip().rstrip('》。；;,.')).is_file()
            add('missing_pdf' if absent else 'identity', text)
            continue
        if any((pattern in lowered for pattern in (p.lower() for p in IDENTITY_PATTERNS))):
            add('identity', text)
            continue
        if any((pattern in text for pattern in USER_MODIFIED_PATTERNS)):
            add('user_modified', text)
            continue
        if '刊名不在缩写表' in text or 'publicationtitle' in lowered:
            add('legacy_format', text)
            continue
        add('other', text)
    if not categories:
        return []
    return [{'category': category, 'label': CATEGORIES[category], 'evidence': values} for category, values in sorted(categories.items())]

def _fingerprints(paper):
    evidence = {}
    if isinstance(paper, dict):
        if isinstance(paper.get('source_sha256'), str):
            evidence['source_sha256'] = paper['source_sha256']
        hashes = (paper.get('qa') or {}).get('metrics', {}).get('input_sha256', {})
        if isinstance(hashes, dict):
            evidence['content_sha256'] = {str(k): v for k, v in hashes.items()}
    return evidence

def record_result(ledger, state):
    status = state.get('status')
    if status not in ('completed', 'partial', 'blocked', 'attention'):
        return ledger
    papers = {p.get('key'): p for p in state.get('papers', []) if isinstance(p, dict)}
    stamp = dt.datetime.now().isoformat(timespec='seconds')
    for key in state.get('requested', []):
        if not re.fullmatch('[A-Z0-9]{8}', key or ''):
            continue
        paper = papers.get(key, {})
        entry = ledger['items'].setdefault(key, {})
        previous = dict(entry)
        entry.update(citekey=paper.get('citekey') or entry.get('citekey', ''), title=paper.get('title') or entry.get('title', ''), run_id=state.get('id'), updated=stamp)
        if paper.get('qa', {}).get('ok') is True and paper.get('index'):
            entry['status'] = 'complete'
            entry['reasons'] = []
        elif paper:
            verdict = 'failed' if paper.get('exit_code') not in (None, 75) or paper.get('qa', {}).get('ok') is False else 'incomplete'
            if verdict == 'failed':
                qa = paper.get('qa') if isinstance(paper.get('qa'), dict) else {}
                codes = sorted({str(e.get('code')) for e in qa.get('errors', []) if isinstance(e, dict) and e.get('code')})
                entry['status'] = 'needs_repair'
                entry['reasons'] = ['产物验收未通过：' + ','.join(codes)] if codes else [f"worker 退出码 {paper.get('exit_code')}"]
                entry['classification'] = (classify_qa(qa) or []) + (classify_log(paper.get('qc_log', [])) or [])
            elif previous.get('status') not in ('needs_repair', 'missing_pdf', 'complete', 'excluded'):
                entry['status'] = 'incomplete'
        fingerprints = _fingerprints(paper)
        if fingerprints:
            entry['evidence'] = fingerprints
        elif 'evidence' not in entry:
            entry['evidence'] = {}
    for item in state.get('deferred', []) + state.get('needs_action', []):
        if not isinstance(item, dict):
            continue
        key = item.get('key')
        if not re.fullmatch('[A-Z0-9]{8}', key or ''):
            continue
        entry = ledger['items'].setdefault(key, {})
        entry.update(citekey=item.get('citekey') or entry.get('citekey', ''), run_id=state.get('id'), updated=stamp, reasons=[str(x)[:300] for x in item.get('blocked', item.get('reasons', []))])
        categories = classify_reasons(entry['reasons'])
        names = {row['category'] for row in categories}
        if entry.get('status') in ('complete', 'excluded'):
            continue
        entry['status'] = 'missing_pdf' if 'missing_pdf' in names else 'needs_repair'
        if categories:
            entry['classification'] = categories
    return ledger

def record_disposition(ledger, key, *, action, resulting_status, reasons=(), release_hold=False, evidence=None):
    if not re.fullmatch('[A-Z0-9]{8}', key or ''):
        raise ValueError('invalid item key')
    if resulting_status not in STATUSES:
        raise ValueError('invalid resulting status')
    entry = ledger['items'].setdefault(key, {'status': 'new', 'citekey': '', 'title': '', 'reasons': [], 'dispositions': [], 'updated': None})
    entry['dispositions'] = entry.get('dispositions', [])
    entry['dispositions'].append({'action': action, 'status': resulting_status, 'reasons': [str(x)[:300] for x in reasons], 'at': dt.datetime.now().isoformat(timespec='seconds'), 'previous_status': entry.get('status', 'new'), 'release_hold': bool(release_hold)})
    entry['status'] = resulting_status
    entry['updated'] = dt.datetime.now().isoformat(timespec='seconds')
    entry['hold_released'] = bool(entry.get('hold_released') or release_hold)
    if evidence:
        entry['evidence'] = {**(entry.get('evidence') or {}), **evidence}
    return ledger

def undo_disposition(ledger, key):
    if not re.fullmatch('[A-Z0-9]{8}', key or ''):
        raise ValueError('invalid item key')
    entry = ledger['items'].get(key)
    if not isinstance(entry, dict) or not entry.get('dispositions'):
        raise RuntimeError('该条目没有可撤销的处置记录')
    last = entry['dispositions'].pop()
    entry['status'] = last.get('previous_status', 'new')
    entry['updated'] = dt.datetime.now().isoformat(timespec='seconds')
    if not entry['dispositions']:
        entry.pop('hold_released', None)
    return ledger
