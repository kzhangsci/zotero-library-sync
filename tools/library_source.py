# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
from collections.abc import Mapping
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit
__all__ = ['inspect_pdf', 'find_previous_source', 'compare_sources', 'normalize_doi']
_SHA = re.compile('[0-9a-fA-F]{64}\\Z')
_KEY = re.compile('[A-Z0-9]{8}\\Z')
_HEADER_LIMIT = 65536
_JSON_LIMIT = 16 * 1024 * 1024
_TEXT_LIMIT = 32 * 1024 * 1024
_HISTORY_BUDGET = 128 * 1024 * 1024
_RUN_LIMIT = 512
_LIBRARY_FILE_LIMIT = 20000
_ENDINGS = {'references': 'references?(?: and notes| cited)?|reference list|bibliography|literature cited|works cited|notes and references|参考文献|引用文献', 'conclusions': 'conclusions?|concluding remarks|summary(?: and conclusions?)?|discussion and conclusions?|结论|结语|总结'}
_HEADINGS = {**_ENDINGS, 'introduction': 'introduction|background|引言', 'methods': '(?:materials? and )?methods?|methodology|方法', 'results': 'results(?: and discussion)?|结果', 'discussion': 'discussion|讨论', 'appendix': 'appendi(?:x|ces)(?: [A-Z])?|supporting information|supplementary material'}
_HEADING_RX = {kind: re.compile('^(?:(?:\\d+(?:\\.\\d+)*[.)]?|[IVXLCDM]+[.)])\\s*)?(?:' + pattern + ')[.:]?\\s*$', re.I) for kind, pattern in _HEADINGS.items()}

def _text(value):
    return value.strip() if isinstance(value, str) else ''

def normalize_doi(value):
    value = _text(value)
    if value.lower().startswith(('https://doi.org/', 'http://doi.org/', 'https://dx.doi.org/', 'http://dx.doi.org/')):
        value = unquote(urlsplit(value).path[1:])
    else:
        value = re.sub('^doi\\s*:\\s*', '', value, flags=re.I)
    return value.casefold() if re.fullmatch('10\\.\\d{4,9}/[^\\s<>]+', value) else ''

def _metadata(value):
    value = value if isinstance(value, Mapping) else {}
    data = value.get('data')
    return {**value, **(data if isinstance(data, Mapping) else {})}

def _pages_info(metadata):
    raw = metadata.get('pages', '')
    raw = str(raw).strip() if raw is not None else ''
    article = next((_text(metadata.get(k)) for k in ('articleNumber', 'article_number', 'article_id') if _text(metadata.get(k))), '')
    value = re.sub('^(?:pages?|pp?\\.?)\\s*', '', raw, flags=re.I)
    value = re.sub('[‐-―−]', '-', value).strip()
    info = {'raw': raw, 'article_id': article, 'kind': 'missing', 'expected_count': None}
    if article and value.casefold() == article.casefold():
        info['kind'] = 'article_id'
    elif (match := re.fullmatch('([A-Za-z]*)(\\d+)\\s*-\\s*([A-Za-z]*)(\\d+)', value)):
        prefix, first, second_prefix, last = match.groups()
        start, end = (int(first), int(last))
        if len(last) < len(first):
            scale = 10 ** len(last)
            end += start // scale * scale
            if end < start:
                end += scale
        if second_prefix and second_prefix.casefold() != prefix.casefold() or end < start:
            info['kind'] = 'unparsed'
        else:
            info.update(kind='range', start=start, end=end, prefix=prefix, expected_count=end - start + 1)
    elif value:
        info['kind'] = 'single_value_or_article_id' if re.fullmatch('[A-Za-z]*\\d+', value) else 'unparsed'
    return info

def _pdf_issue(result, code, message, *, blocked=False, **evidence):
    severity = 'blocked' if blocked else 'warning'
    result['blocked' if blocked else 'warnings'].append(f'{code}: {message}')
    result['issues'].append({'code': code, 'severity': severity, **evidence})

def _pdf_result(result):
    result['ok'] = not result['blocked']
    result['status'] = 'blocked' if result['blocked'] else 'warning' if result['warnings'] else 'ok'
    result['metrics'].update(sha256=result['sha256'], page_count=result['page_count'], likely_incomplete=result['likely_incomplete'])
    return result

def inspect_pdf(path, metadata=None):
    metadata = _metadata(metadata)
    pages = _pages_info(metadata)
    result = {'sha256': '', 'page_count': None, 'likely_incomplete': False, 'blocked': [], 'warnings': [], 'issues': [], 'metrics': {'size_bytes': None, 'pages_metadata': pages, 'headings': [], 'lastpage': None, 'text_pages': 0, 'unsearchable_pages': [], 'pages_checked': 0, 'encrypted': None, 'repaired': None, 'ending_headings': [], 'fulltext_verified': False, 'scope': 'byte hash, PDF parsing and text extraction; no OCR/rendering'}}
    try:
        raw = Path(path).read_bytes()
    except (OSError, ValueError) as exc:
        _pdf_issue(result, 'pdf_read_failed', type(exc).__name__, blocked=True)
        return _pdf_result(result)
    result['sha256'] = hashlib.sha256(raw).hexdigest()
    result['metrics']['size_bytes'] = len(raw)
    if not raw:
        _pdf_issue(result, 'pdf_empty', 'File contains no bytes.', blocked=True)
        return _pdf_result(result)
    try:
        import fitz
    except ImportError:
        _pdf_issue(result, 'fitz_unavailable', 'PyMuPDF is required; PDF not inspected.', blocked=True)
        return _pdf_result(result)
    try:
        document = fitz.open(stream=raw, filetype='pdf')
    except Exception as exc:
        _pdf_issue(result, 'pdf_open_failed', type(exc).__name__, blocked=True)
        return _pdf_result(result)
    try:
        with document:
            metrics = result['metrics']
            metrics['encrypted'] = bool(document.needs_pass)
            metrics['repaired'] = bool(document.is_repaired)
            if document.needs_pass:
                _pdf_issue(result, 'pdf_encrypted', 'Password required; content was not inspected.', blocked=True)
                return _pdf_result(result)
            result['page_count'] = count = document.page_count
            if not count:
                _pdf_issue(result, 'pdf_no_pages', 'PDF has no readable pages.', blocked=True)
                return _pdf_result(result)
            if document.is_repaired:
                _pdf_issue(result, 'pdf_repaired_in_memory', 'Parser recovered damaged structure; original bytes were not modified.')
            for number in range(count):
                try:
                    page_text = document.load_page(number).get_text('text')
                except Exception as exc:
                    _pdf_issue(result, 'pdf_page_unreadable', type(exc).__name__, blocked=True, page=number + 1)
                    continue
                metrics['pages_checked'] += 1
                headings = []
                for line in page_text.splitlines():
                    line = ' '.join(line.split())
                    for kind, rx in _HEADING_RX.items():
                        if rx.fullmatch(line):
                            heading = {'page': number + 1, 'kind': kind, 'text': line}
                            headings.append(heading)
                            metrics['headings'].append(heading)
                            break
                if page_text.strip():
                    metrics['text_pages'] += 1
                else:
                    metrics['unsearchable_pages'].append(number + 1)
                if number == count - 1:
                    metrics['lastpage'] = {'page': count, 'text_chars': len(page_text.strip()), 'headings': headings, 'text_tail': page_text.strip()[-500:]}
            endings = [h for h in metrics['headings'] if h['kind'] in _ENDINGS]
            metrics['ending_headings'] = endings
            if metrics['unsearchable_pages']:
                _pdf_issue(result, 'pdf_text_unavailable', 'Some pages have no extractable text; completeness is uncertain.', pages=metrics['unsearchable_pages'])
            if not endings:
                _pdf_issue(result, 'ending_not_detected', 'No standalone references/conclusions heading found; absence alone is not truncation evidence.')
            brief_kind = ' '.join((_text(metadata.get(k)) for k in ('articleType', 'article_type', 'genre')))
            editorial = re.match('^(?:editorial|erratum|corrigendum|correction\\b|retraction|book review|obituary|news\\b|letter to the editor)\\b', _text(metadata.get('title')), re.I) or re.search('\\b(?:editorial|erratum|corrigendum|correction|retraction|obituary)\\b', brief_kind, re.I)
            if metadata.get('itemType') == 'journalArticle' and count <= 2 and (not endings) and (not editorial):
                _pdf_issue(result, 'short_pdf_possible_preview', "This journal PDF has at most two pages and no detected references/conclusions heading; it may be a Connector preview or truncated download. Legitimate short articles also exist; this warning does not prove incompleteness. Check the publisher's full text/page range.", actual_pages=count, ending_heading_detected=False)
            expected = pages['expected_count']
            if expected is not None and count < expected:
                result['likely_incomplete'] = True
                readable = metrics['pages_checked'] == count and (not metrics['unsearchable_pages'])
                _pdf_issue(result, 'likely_incomplete', 'PDF has fewer physical pages than the explicit metadata range.', blocked=bool(readable and (not endings)), expected_pages=expected, actual_pages=count, ending_heading_detected=bool(endings))
            elif pages['kind'] == 'unparsed':
                _pdf_issue(result, 'pages_metadata_unparsed', 'Page metadata is not a supported single range; no page-count inference made.')
    except Exception as exc:
        _pdf_issue(result, 'pdf_inspection_failed', type(exc).__name__, blocked=True)
    return _pdf_result(result)

def _identity(record):
    record = record if isinstance(record, Mapping) else {}
    nested = record.get('source')
    source = nested if isinstance(nested, Mapping) else {}
    data = _metadata(record)
    data = {**source, **data}
    key = next((_text(data.get(k)) for k in ('item_key', 'zotero_key', 'key') if _text(data.get(k))), '')
    digest = _text(data.get('source_sha256') or source.get('sha256'))
    primary = _text(data.get('primary_key') or data.get('attachment_key'))
    return {'item_key': key, 'citekey': _text(data.get('citekey') or data.get('citationKey')), 'doi': normalize_doi(data.get('doi') or data.get('DOI')), 'source_sha256': digest.lower() if _SHA.fullmatch(digest) else '', 'primary_key': primary, 'primary_pdf': _text(data.get('primary_pdf')), 'source_pdf': _text(data.get('source_pdf')), 'source_path': _text(data.get('source_path'))}

def _identity_conflicts(record):
    record = record if isinstance(record, Mapping) else {}
    layers = [record] + [record[k] for k in ('source', 'data') if isinstance(record.get(k), Mapping)]
    keys = {_text(r.get(k)) for r in layers for k in ('item_key', 'zotero_key', 'key') if _text(r.get(k))}
    dois = {normalize_doi(r.get(k)) for r in layers for k in ('doi', 'DOI') if normalize_doi(r.get(k))}
    return (['item_key'] if len(keys) > 1 or any((not _KEY.fullmatch(k) for k in keys)) else []) + (['doi'] if len(dois) > 1 else [])

def compare_sources(previous, current):
    old, new = (_identity(previous), _identity(current))
    content = old['source_sha256'] != new['source_sha256'] if old['source_sha256'] and new['source_sha256'] else None
    identity_changes = [field for field in ('item_key', 'primary_key') if old[field] and new[field] and (old[field] != new[field])]
    identity_known = all((old[k] and new[k] for k in ('item_key', 'primary_key')))
    identity = True if identity_changes else False if identity_known else None
    reasons = [f'{field}_changed' for field in identity_changes]
    if content is True:
        reasons.append('source_sha256_changed')
    if content is None:
        reasons.append('source_sha256_unverified')
    if identity is None:
        reasons.append('source_identity_unverified')
    changed = True if content is True or identity is True else False if content is False and identity is False else None
    if _identity_conflicts(previous) or _identity_conflicts(current):
        reasons.append('source_identity_conflict')
        identity, changed = (None, None)
    return {'changed': changed, 'content_changed': content, 'identity_changed': identity, 'review_required': changed is not False, 'reasons': reasons}

def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result

class _ReadLimit(ValueError):
    pass

def _read_bytes(path, limit, budget=None):
    allowed = min(limit, budget[0]) if budget is not None else limit
    with path.open('rb') as stream:
        raw = stream.read(allowed + 1)
    if budget is not None:
        budget[0] = max(0, budget[0] - len(raw))
    if len(raw) > allowed:
        raise _ReadLimit('source read limit exceeded')
    return raw

def _json(path, budget=None):
    return json.loads(_read_bytes(path, _JSON_LIMIT, budget).decode('utf-8-sig'), object_pairs_hook=_unique)

def _frontmatter(raw):
    match = re.match(b'\\A(?:\\xef\\xbb\\xbf)?---\\r?\\n(.{0,65536}?)\\r?\\n---(?:\\r?\\n|$)', raw, re.S)
    if not match:
        raise ValueError('missing, oversized or unclosed frontmatter')
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        return _unique(((loader.construct_object(k), loader.construct_object(v)) for k, v in node.value))
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    value = yaml.load(match[1].decode('utf-8'), Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ValueError('frontmatter is not a mapping')
    return value

def _safe(path, root):
    path, root = (Path(path).absolute(), Path(root).absolute())
    if not path.is_relative_to(root):
        return False
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            return False
    return path.resolve().is_relative_to(root.resolve())

def _stamp(state):
    for name in ('finished', 'started'):
        try:
            stamp = dt.datetime.fromisoformat(_text(state.get(name)).replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            return stamp.timestamp()
        except ValueError:
            pass
    try:
        return dt.datetime.strptime(_text(state.get('id'))[:15], '%Y%m%d_%H%M%S').replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return 0

def _run_records(runs, warnings, *, current=None, anchors=(), resolve_doi=True):
    records = []
    if not runs.exists():
        return records
    if not _safe(runs, runs) or not runs.is_dir():
        warnings.append('runs_unreadable: explicit runs root is not a regular directory')
        return records
    budget = [_HISTORY_BUDGET]
    for number, path in enumerate(sorted(runs.glob('*/state.json'))):
        if number >= _RUN_LIMIT or not budget[0]:
            warnings.append('source_lookup_limit: history scan limit reached')
            break
        try:
            if not _safe(path, runs):
                raise ValueError('linked state')
            state = _json(path, budget)
            if not isinstance(state, dict) or not isinstance(state.get('papers', []), list):
                raise ValueError('invalid state')
            papers = state.get('papers', [])
            relevant = [paper for paper in papers if isinstance(paper, dict) and (current is None or _match(_identity(paper), current) or _identity(paper)['item_key'] in anchors)]
            if current is not None and (not relevant) and (not resolve_doi):
                continue
            snapshot, by_key = (path.with_name('snapshot.json'), {})
            need_snapshot = current is None or (resolve_doi and any((isinstance(paper, dict) and (not _identity(paper)['doi']) for paper in papers)))
            if need_snapshot and snapshot.exists():
                if not _safe(snapshot, runs):
                    raise ValueError('linked snapshot')
                raw = _json(snapshot, budget)
                if not isinstance(raw, dict) or not isinstance(raw.get('items'), list):
                    raise ValueError('invalid snapshot')
                for item in raw['items']:
                    ident = _identity(item)
                    if ident['item_key'] in by_key:
                        raise ValueError('duplicate snapshot identity')
                    by_key[ident['item_key']] = ident
            for paper in papers:
                if not isinstance(paper, dict):
                    warnings.append(f'run_paper_unreadable: {path.parent.name}')
                    continue
                ident = _identity(paper)
                supplement = by_key.get(ident['item_key'], {})
                conflicts = _identity_conflicts(paper) + [k for k in ('doi', 'citekey') if ident[k] and supplement.get(k) and (ident[k] != supplement[k])]
                for key in ('doi', 'citekey'):
                    ident[key] = ident[key] or supplement.get(key, '')
                if current is not None and (not (_match(ident, current) or ident['item_key'] in anchors)):
                    continue
                qa = paper.get('qa') if isinstance(paper.get('qa'), dict) else {}
                metrics = qa.get('metrics') if isinstance(qa.get('metrics'), dict) else {}
                hashes = metrics.get('input_sha256', {})
                hashes = {k: v.lower() for k, v in hashes.items() if isinstance(k, str) and isinstance(v, str) and _SHA.fullmatch(v)} if isinstance(hashes, dict) else {}
                frozen = state.get('qa_frozen', {})
                frozen = {k: v.lower() for k, v in frozen.items() if isinstance(k, str) and isinstance(v, str) and _SHA.fullmatch(v)} if isinstance(frozen, dict) else {}
                records.append({**ident, 'kind': 'run', 'path': str(path.absolute()), 'run_id': _text(state.get('id')), 'run_status': _text(state.get('status')), 'resume_of': _text(state.get('resume_of')), 'paper_status': _text(paper.get('status')), 'qa_ok': qa.get('ok') is True, 'completed': state.get('status') == 'completed' and paper.get('exit_code') == 0 and (qa.get('ok') is True), 'recorded_time': _stamp(state), 'qa_hashes': hashes, 'qa_frozen': frozen, 'identity_conflicts': conflicts})
        except _ReadLimit:
            warnings.append(f'source_lookup_limit: history record {path.parent.name} exceeded read limit')
        except Exception as exc:
            warnings.append(f'run_record_unreadable: {path.name} in {path.parent.name} ({type(exc).__name__})')
    return records

def _component(value):
    return bool(value and value not in {'.', '..'} and (not value.endswith(('.', ' '))) and (not re.search('[<>:"/\\\\|?*\\x00-\\x1f]', value)))

def _linked_entry(entry):
    return entry.is_symlink() or bool(getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))

def _header(path):
    with path.open('rb') as stream:
        first = stream.readline(_HEADER_LIMIT + 1)
        if first.lstrip(b'\xef\xbb\xbf').strip() != b'---':
            return first
        parts, size = ([first], len(first))
        while size <= _HEADER_LIMIT:
            line = stream.readline(_HEADER_LIMIT + 1 - size)
            if not line:
                break
            parts.append(line)
            size += len(line)
            if line.strip() == b'---':
                break
        return b''.join(parts)

def _might_match(raw, current):
    text = unquote(raw.decode('utf-8', errors='replace')).casefold()
    return any((value and value.casefold() in text for value in (current['item_key'], current['doi']))) or '\\u' in text

def _library_records(library, warnings, *, current=None, folder=None, errors=None):
    errors = errors if errors is not None else warnings
    records, papers = ([], library / 'papers')
    if not library.exists():
        errors.append('library_missing: explicit Library root does not exist')
        return records
    if not _safe(library, library) or not library.is_dir():
        errors.append('library_unreadable: explicit Library root is not a regular directory')
        return records
    if not papers.exists():
        return records
    if not _safe(papers, library):
        errors.append('library_link_refused: papers directory is a link')
        return records
    if folder is not None:
        if not folder.exists():
            return records
        if not _safe(folder, library) or not folder.is_dir():
            errors.append(f'library_link_refused: {folder.name}')
            return records
        for path in sorted(folder.glob('*.md')):
            if re.search('_(?:EN|MD|ZH|Note)\\.md$', path.name):
                try:
                    if not _safe(path, library):
                        raise ValueError('linked markdown')
                    raw = _read_bytes(path, _TEXT_LIMIT)
                    front = _frontmatter(raw)
                    records.append({**_identity(front), 'kind': 'library', 'path': str(path.absolute()), 'folder': str(folder.absolute()), 'recorded_time': 0, 'file_sha256': hashlib.sha256(raw).hexdigest(), 'identity_conflicts': _identity_conflicts(front)})
                except Exception as exc:
                    errors.append(f'library_record_unreadable: {folder.name}/{path.name} ({type(exc).__name__})')
        return records
    scanned = 0
    with os.scandir(papers) as entries:
        directories = sorted(entries, key=lambda e: e.name)
    for entry in directories:
        folder = Path(entry.path)
        try:
            if _linked_entry(entry):
                warnings.append(f'library_link_refused: {entry.name}')
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            with os.scandir(folder) as children:
                paths = sorted(children, key=lambda e: e.name)
        except OSError as exc:
            warnings.append(f'library_scan_unreadable: {entry.name} ({type(exc).__name__})')
            continue
        for child in paths:
            if not re.search('_(?:EN|MD|ZH|Note)\\.md$', child.name):
                continue
            scanned += 1
            if scanned > _LIBRARY_FILE_LIMIT:
                errors.append('source_lookup_limit: Library header scan limit reached')
                return records
            try:
                if _linked_entry(child) or not child.is_file(follow_symlinks=False):
                    continue
                raw = _header(Path(child.path))
                if current is not None and (not _might_match(raw, current)):
                    continue
                front = _frontmatter(raw)
                if current is not None and (not _match(_identity(front), current)):
                    continue
                records.extend(_library_records(library, warnings, folder=folder, errors=errors))
                break
            except Exception as exc:
                warnings.append(f'library_record_unreadable: {folder.name}/{child.name} ({type(exc).__name__})')
    return records

def _match(old, current):
    if current['item_key'] and old['item_key'] == current['item_key']:
        return 'item_key'
    if current['doi'] and old['doi'] == current['doi']:
        return 'doi'
    return ''

def _files(folder, library, warnings):
    result = {}
    pending = [Path(folder)]
    while pending:
        directory = pending.pop()
        try:
            for path in sorted(directory.iterdir()):
                if not _safe(path, library):
                    warnings.append(f'library_link_refused: {path.name}')
                elif path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                            digest.update(chunk)
                    result[str(path.absolute())] = digest.hexdigest()
        except OSError as exc:
            warnings.append(f'library_files_unreadable: {directory.name} ({type(exc).__name__})')
    return result

def _unresolved(warnings, blocked, candidates=()):
    return {'citekey': '', 'item_key': '', 'doi': '', 'source_sha256': '', 'primary_key': '', 'files': {}, 'match': 'unresolved', 'review_required': True, 'old_item_live': None, 'blocked': list(blocked), 'warnings': list(warnings), 'records': [], 'candidates': list(candidates), 'ambiguous': True}

def find_previous_source(library, runs, parent, *, include_run_receipts=False):
    library, runs = (Path(library).absolute(), Path(runs).absolute())
    current = _identity(parent)
    if current['item_key'] and (not _KEY.fullmatch(current['item_key'])):
        return _unresolved([], ['invalid_parent_item_key'])
    if not current['item_key'] and (not current['doi']):
        return _unresolved([], ['parent_identity_missing'])
    if _identity_conflicts(parent):
        return _unresolved([], ['parent_identity_conflict'])
    warnings, lookup_errors = ([], [])
    live = parent.get('live_item_keys') if isinstance(parent, Mapping) else None
    if live is not None:
        if not isinstance(live, (list, tuple, set, frozenset)) or not all((isinstance(k, str) and _KEY.fullmatch(k) for k in live)):
            return _unresolved([], ['invalid_live_item_keys'])
        live = set(live)
        if current['item_key'] and current['item_key'] not in live:
            return _unresolved([], ['live_inventory_missing_current_item'])
    try:
        records = []
        if _component(current['citekey']):
            records = _library_records(library, warnings, errors=lookup_errors, folder=library / 'papers' / current['citekey'])
        exact_fulltext = any((_match(r, current) == 'item_key' and re.search('_(?:EN|MD)\\.md$', r['path']) for r in records))
        if not exact_fulltext:
            records = _library_records(library, warnings, current=current, errors=lookup_errors)
        matched = [r for r in records if _match(r, current)]
        anchors = {r['item_key'] for r in matched if _KEY.fullmatch(r['item_key'])}
        fulltext = [r for r in matched if re.search('_(?:EN|MD)\\.md$', r['path'])]
        direct = fulltext and all((r['source_sha256'] and (not r['identity_conflicts']) and _KEY.fullmatch(r['item_key']) for r in fulltext))
        if not direct or include_run_receipts:
            records += _run_records(runs, warnings, current=current, anchors=anchors, resolve_doi=not matched)
    except OSError as exc:
        return _unresolved([f'source_lookup_failed: {type(exc).__name__}'], ['source_lookup_incomplete'])
    matches = [r for r in records if _match(r, current)]
    if not matches:
        diagnostics = warnings + lookup_errors
        return _unresolved(diagnostics, ['source_lookup_incomplete']) if diagnostics else None
    anchors = {r['item_key'] for r in matches if _KEY.fullmatch(r['item_key'])}
    matches = [r for r in records if _match(r, current) or r['item_key'] in anchors]
    groups = {}
    for record in matches:
        identity = record['item_key'] or 'doi:' + record['doi'] + ':' + record['citekey']
        groups.setdefault(identity, []).append(record)
    candidates = []
    for group in groups.values():
        local_warnings, blocked = ([], [])
        folders = sorted({r['folder'] for r in group if r['kind'] == 'library'})
        files = {}
        for folder in folders:
            files.update(_files(folder, library, local_warnings))
        for other in records:
            if other.get('folder') in folders:
                if other['item_key'] and group[0]['item_key'] and (other['item_key'] != group[0]['item_key']):
                    blocked.append('library_folder_identity_conflict')
                if other['doi'] and current['doi'] and (other['doi'] != current['doi']):
                    blocked.append('library_folder_doi_conflict')
        fulltext = [r for r in group if r['kind'] == 'library' and re.search('_(?:EN|MD)\\.md$', r['path'])]
        for record in group:
            if record['kind'] == 'library' and record['file_sha256'] != files.get(record['path']):
                blocked.append('library_changed_during_lookup')
            qa_hashes, frozen = (record.get('qa_hashes', {}), record.get('qa_frozen', {}))
            record['library_hash_verified'] = bool(fulltext) and all((files.get(r['path']) == r['file_sha256'] and (qa_hashes.get(Path(r['path']).name) == r['file_sha256'] or frozen.get(r['path']) == r['file_sha256'] or frozen.get(Path(r['path']).as_posix()) == r['file_sha256']) for r in fulltext))
        direct = [r for r in fulltext if r['source_sha256'] and _KEY.fullmatch(r['item_key']) and (not r['identity_conflicts']) and (files.get(r['path']) == r['file_sha256'])]
        if len(direct) != len(fulltext):
            direct = []
        evidence = direct or [r for r in group if r['source_sha256'] and r['library_hash_verified']]
        evidence.sort(key=lambda r: r['recorded_time'], reverse=True)
        selected = evidence[0] if evidence else next((r for r in group if r['kind'] == 'library'), group[0])
        ambiguous_hash = len({r['source_sha256'] for r in evidence}) > 1
        if ambiguous_hash:
            blocked.append('historical_source_hash_ambiguous')
        if any((r['identity_conflicts'] for r in group)):
            blocked.append('historical_identity_conflict')
        if any((r['doi'] and current['doi'] and (r['doi'] != current['doi']) for r in group)):
            blocked.append('historical_doi_conflict')
        aliases = sorted({r['citekey'] for r in group if r['citekey']})
        folder_keys = sorted({r['citekey'] for r in group if r['kind'] == 'library' and r['citekey']})
        old_key = selected['item_key']
        match = 'item_key' if old_key and old_key == current['item_key'] else 'doi'
        old_live = old_key in live if live is not None and old_key else None
        if match == 'doi':
            local_warnings.append('doi_identity_review: exact DOI does not authorize item merge or replacement')
            if old_live:
                blocked.append('previous_item_still_live')
        if not evidence:
            local_warnings.append('source_sha256_unknown: no current EN/MD provenance or matching historical QA hash; redo conservatively')
        if len(folders) > 1:
            local_warnings.append('multiple_library_folders: preserve and review every folder')
        if not folders:
            local_warnings.append('library_files_not_found: historical receipt only')
        if local_warnings and any((w.startswith(('library_link_refused', 'library_files_unreadable')) for w in local_warnings)):
            blocked.append('source_lookup_incomplete')
        identity_ambiguous = ambiguous_hash or any((code != 'previous_item_still_live' for code in blocked))
        candidates.append({**{k: selected[k] for k in ('item_key', 'primary_key', 'primary_pdf', 'source_pdf', 'source_path')}, 'doi': selected['doi'] or next((r['doi'] for r in group if r['doi']), ''), 'citekey': folder_keys[0] if len(folder_keys) == 1 else selected['citekey'], 'source_sha256': selected['source_sha256'] if evidence and (not identity_ambiguous) else '', 'files': files, 'folders': folders, 'citekey_aliases': aliases, 'match': match, 'old_item_live': old_live, 'library_hash_verified': selected.get('library_hash_verified', False), 'review_required': bool(blocked or local_warnings or match == 'doi'), 'blocked': sorted(set(blocked)), 'warnings': local_warnings, 'records': group, 'ambiguous': identity_ambiguous})
    exact = [c for c in candidates if c['match'] == 'item_key']
    preferred = exact or candidates
    if len(preferred) != 1:
        return _unresolved(warnings + lookup_errors, ['ambiguous_previous_sources'], candidates)
    result = preferred[0]
    result['warnings'] = warnings + lookup_errors + result['warnings']
    if warnings or lookup_errors:
        result['review_required'] = True
    if lookup_errors or (warnings and (not result['files'])):
        result['ambiguous'] = True
        result['source_sha256'] = ''
        result['blocked'] = sorted(set(result['blocked'] + ['source_lookup_incomplete']))
    elif any((w.startswith('source_lookup_limit:') for w in warnings)):
        result['source_sha256'] = ''
    result['candidates'] = [c for c in candidates if c is not result]
    return result
