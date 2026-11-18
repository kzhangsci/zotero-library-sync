# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import copy
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def library_files(folder):
    folder = Path(folder)
    result = {}
    if not folder.exists():
        return result
    for path in folder.rglob('*'):
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise RuntimeError('文献目录包含链接，不能自动归档')
        if path.is_file():
            if path.name.startswith('.sync_') and path.suffix == '.lock':
                raise RuntimeError('文献仍有同步锁，请等待当前任务结束')
            result[str(path)] = _digest(path)
    return result

def _doi(value):
    return re.sub('^https?://(?:dx\\.)?doi\\.org/|^doi:\\s*', '', str(value or '').strip(), flags=re.I).rstrip('.').lower()

def _reanchor(data, old, new):
    bom = b'\xef\xbb\xbf' if data.startswith(b'\xef\xbb\xbf') else b''
    content = data[len(bom):]
    match = re.match(b'\\A---\\r?\\n(.*?)\\r?\\n---(?:\\r?\\n|$)', content, re.S)
    if not match:
        raise RuntimeError('原笔记/原文缺少完整元数据，停止换绑')
    header = match[1]
    anchors = re.findall(b'(?m)^zotero_key:', header)
    pattern = b'(?m)^(zotero_key:[ \\t]*)[\\\'"]?' + re.escape(old.encode()) + b'[\\\'"]?([ \\t]*\\r?$)'
    updated, n = re.subn(pattern, lambda m: m[1] + new.encode() + m[2], header)
    if len(anchors) != 1 or n != 1:
        raise RuntimeError('原笔记/原文的 Zotero 锚点不匹配，停止换绑')
    content = content[:match.start(1)] + updated + content[match.end(1):]
    return bom + content.replace(('zotero://select/library/items/' + old).encode(), ('zotero://select/library/items/' + new).encode())

def plan_refresh(client, key, *, primary_key=None):
    import library_intake as intake
    from library_source import find_previous_source
    plan = {'key': key, 'citekey': '', 'blocked': [], 'warnings': [], 'actions': [], 'refresh_source': True}
    try:
        parent = intake._get(client, key)
        children = intake._children(client, parent)
        pdfs = [a for a in children if a['data'].get('contentType') == 'application/pdf' or a['data'].get('filename', '').lower().endswith('.pdf')]
        if not pdfs:
            raise RuntimeError('没有可用 PDF；请先补充全文附件')
        roles, unknown = ({}, [])
        for a in pdfs:
            try:
                roles[a['key']] = intake._infer(a, parent, len(pdfs), {})
            except intake.IntakeBlocked:
                unknown.append(a['key'])
        if primary_key:
            if primary_key not in {a['key'] for a in pdfs}:
                raise RuntimeError('选中的正本附件不属于该文献')
            if roles.get(primary_key, ('other',))[0] not in ('other', 'primary'):
                raise RuntimeError('批注本、历史版本及补充材料不能选作新正本')
            chosen = primary_key
        elif len(unknown) == 1:
            chosen = unknown[0]
        elif unknown:
            raise RuntimeError('有多份新 PDF，无法确定全文正本；请在 Zotero 中只选择目标 PDF 附件后重新预演')
        else:
            primaries = [k for k, (role, _) in roles.items() if role == 'primary']
            if len(primaries) != 1:
                raise RuntimeError('无法唯一确定正本；请在 Zotero 中选择新全文 PDF 后重新预演')
            chosen = primaries[0]
        previous = find_previous_source(intake.LIB, intake.REPORTS / '_cache' / 'library_sync', parent)
        if previous and (previous.get('ambiguous') or previous.get('blocked')):
            raise RuntimeError('旧文献来源不能唯一可靠核实：' + '; '.join(previous.get('blocked', [])))
        current_ck = parent['data'].get('citationKey', '')
        old_ck = (previous or {}).get('citekey', '')
        old_key = (previous or {}).get('item_key', key)
        if old_key != key:
            live = client.list_all('items/top')
            if any((x['key'] == old_key and (not x['data'].get('deleted')) for x in live)):
                raise RuntimeError('旧条目仍在 Zotero 有效库中；先处理重复条目，不能自动换锚点')
            if not _doi(parent['data'].get('DOI')) or _doi(parent['data'].get('DOI')) != _doi(previous.get('doi')):
                raise RuntimeError('重新添加的条目缺少一致 DOI，不能自动绑定旧笔记')
        source = next((a for a in pdfs if a['key'] == chosen))
        digest = _digest(intake._path(intake.zotero_data_dir() / 'storage', source))
        old_digest = (previous or {}).get('source_sha256')
        changed = bool(previous) and (not old_digest or old_digest != digest)
        old_primaries = [a for a in pdfs if a['key'] != chosen and roles.get(a['key'], ('other',))[0] == 'primary']
        replaces_pdf = any((_digest(intake._path(intake.zotero_data_dir() / 'storage', a)) != digest for a in old_primaries)) if old_primaries else changed
        hints = {chosen: {'role': 'primary', 'suffix': '.pdf'}}
        archive = []
        for ak, (role, suffix) in roles.items():
            if ak == chosen:
                continue
            if role == 'primary' or (role == 'annotated' and replaces_pdf):
                hints[ak] = {'role': 'archive', 'suffix': f'_superseded_{ak}.pdf'}
                archive.append(ak)
        plan = intake._make_plan(client, key, False, hints=hints, allow_redo=True)
        plan['refresh_source'] = True
        plan['refresh_primary_key'] = primary_key
        plan['role_hints'] = hints
        folder = intake.LIB / 'papers' / (old_ck or current_ck)
        files = library_files(folder) if previous else {}
        if files and plan.get('metadata', {}).get('refresh'):
            raise RuntimeError('已有文献目录且元数据将重新生成 citekey；请先单独迁移目录，再更换全文，本次未改附件')
        if old_key != key:
            for suffix in ('Note',) if changed else ('EN', 'MD', 'ZH', 'Note'):
                path = folder / f'{old_ck}_{suffix}.md'
                if path.is_file():
                    _reanchor(path.read_bytes(), old_key, key)
        plan['refresh'] = {'previous': previous, 'files': files, 'folder': str(folder), 'changed': changed, 'reanchor': old_key != key, 'archive_attachments': archive, 'selected_primary': chosen}
        if old_ck and old_ck != current_ck and (not plan.get('metadata', {}).get('refresh')):
            raise RuntimeError('新旧 citekey 不同，需要先单独迁移文献目录；本次未覆盖旧目录')
        plan['note'] = '更换全文：保留旧 PDF/批注为历史版本；备份旧原文、译文和图片，重新解析→质检→翻译；保留笔记' if changed else '重新检查正本；内容未变化时只补缺失步骤，不重复解析或翻译'
        if not old_digest and previous:
            plan['warnings'].append('旧来源没有可核实的 PDF 哈希；本次明确更换模式将重新处理全文，旧产物保留备份')
        if archive:
            plan['warnings'].append('旧附件保留为 _superseded_<附件key>.pdf；其阅读器/嵌入批注不迁移也不删除')
    except Exception as exc:
        plan['blocked'].append(str(exc))
    return plan

def continuation_receipts(previous, *, item_key, source_sha256, folder, current_run_id=None):
    if not isinstance(previous, dict):
        return None
    if previous.get('blocked') or previous.get('ambiguous') or previous.get('review_required'):
        return None
    records = previous.get('records')
    folder_name = Path(folder).name if isinstance(folder, str) else ''
    if not isinstance(records, list) or not folder_name:
        return None
    try:
        resolved = Path(folder).resolve()
    except OSError:
        return None
    if not isinstance(source_sha256, str) or not re.fullmatch('[0-9a-fA-F]{64}', source_sha256):
        return None
    source = source_sha256.lower()
    allowed = {f'{folder_name}_{suffix}.md' for suffix in ('EN', 'MD', 'ZH', 'Note')}
    per_file, usable = ({}, 0)
    for record in records:
        if not isinstance(record, dict) or record.get('kind') != 'run':
            continue
        if record.get('item_key') != item_key:
            continue
        if current_run_id and record.get('run_id') == current_run_id:
            continue
        digest = record.get('source_sha256')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest):
            continue
        if digest.lower() != source:
            continue
        hashes = record.get('qa_hashes')
        if not isinstance(hashes, dict) or not hashes:
            continue
        values = {}
        for name, value in hashes.items():
            if name not in allowed:
                return None
            if not isinstance(value, str) or not re.fullmatch('[0-9a-fA-F]{64}', value):
                return None
            values[name] = value.lower()
        if not any((name.endswith(('_EN.md', '_MD.md')) for name in values)):
            continue
        usable += 1
        for name, value in values.items():
            try:
                path = (resolved / name).resolve()
            except OSError:
                return None
            if path.parent != resolved:
                return None
            per_file.setdefault(os.path.normcase(str(path)), set()).add(value)
    if not usable:
        return None
    digests = {}
    for path, candidates in per_file.items():
        if len(candidates) != 1:
            return None
        digests[path] = next(iter(candidates))
    return digests

def same_source_receipts(plan, current_run_id=None):
    info = plan.get('refresh') or {}
    if info.get('changed') or info.get('reanchor'):
        return None
    return continuation_receipts(info.get('previous'), item_key=plan.get('key'), source_sha256=plan.get('source_sha256'), folder=info.get('folder'), current_run_id=current_run_id)

def assert_refresh(plan, client):
    fresh = plan_refresh(client, plan['key'], primary_key=plan.get('refresh_primary_key'))
    if fresh.get('blocked'):
        raise RuntimeError('; '.join(fresh['blocked']))
    for field in ('refresh', 'role_hints', 'parent', 'children', 'annotations', 'versions', 'hashes', 'metadata'):
        if fresh.get(field) != plan.get(field):
            raise RuntimeError('全文更换预演已变化，请重新预演：' + field)

def archive_generated(plan, result, run):
    import library_intake as intake
    info = plan['refresh']
    previous = info.get('previous')
    if not previous:
        return {}
    folder = Path(info['folder'])
    if folder.name != result['citekey']:
        raise RuntimeError('BBT 新键与原文献目录不同；附件已核验，但旧产物未动，请先迁移目录')
    if not info['changed'] and (not info['reanchor']):
        return {}
    from library_sync import guarded_file
    with contextlib.ExitStack() as stack:
        handles = {}
        for name, expected in sorted(info['files'].items()):
            path = Path(name)
            if not path.resolve().is_relative_to(folder.resolve()):
                raise RuntimeError('预演文件路径越界')
            generated = path.parent == folder and path.name in {folder.name + '_' + s + '.md' for s in ('EN', 'MD', 'ZH')}
            stream = stack.enter_context(guarded_file(path, writable=info['reanchor'], movable=info['changed'] and generated))
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise RuntimeError('预演后原文/译文/笔记/图片发生变化；未归档')
            stream.seek(0)
            handles[path] = stream
        if {str(p) for p in folder.rglob('*') if p.is_file()} != set(info['files']):
            raise RuntimeError('预演后文献目录内容变化；未归档')
        return _archive_locked(plan, result, run, handles)

def _move_open(stream, destination):
    import ctypes
    from ctypes import wintypes
    import msvcrt
    name = str(Path(destination).absolute()).encode('utf-16-le')

    class RenameInfo(ctypes.Structure):
        _fields_ = [('ReplaceIfExists', wintypes.BOOLEAN), ('RootDirectory', wintypes.HANDLE), ('FileNameLength', wintypes.DWORD), ('FileName', ctypes.c_ubyte * len(name))]
    info = RenameInfo(False, None, len(name), (ctypes.c_ubyte * len(name)).from_buffer_copy(name))
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = kernel.SetFileInformationByHandle
    function.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    function.restype = wintypes.BOOL
    if not function(msvcrt.get_osfhandle(stream.fileno()), 3, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())

def _archive_locked(plan, result, run, handles):
    import library_intake as intake
    info = plan['refresh']
    previous = info['previous']
    folder = Path(info['folder'])
    ck = result['citekey']
    note = folder / f'{ck}_Note.md'
    generated = [folder / f'{ck}_{s}.md' for s in ('EN', 'MD', 'ZH')]
    updates = {}
    if info['reanchor']:
        for path in [note] + ([] if info['changed'] else generated):
            if path.is_file():
                stream = handles[path]
                stream.seek(0)
                updates[path] = _reanchor(stream.read(), previous['item_key'], plan['key'])
    target = intake.LIB / '.trash' / 'source_refresh' / Path(run).name / plan['key']
    target = target.resolve()
    if not target.is_relative_to((intake.LIB / '.trash').resolve()) or target.exists():
        raise RuntimeError('全文更换备份目录已存在或不安全；请检查上次记录，未重复操作')
    target.mkdir(parents=True)
    (target / 'manifest.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    owned = {}
    for path, stream in handles.items():
        backup = target / path.relative_to(folder)
        backup.parent.mkdir(parents=True, exist_ok=True)
        stream.seek(0)
        with backup.open('xb') as out:
            shutil.copyfileobj(stream, out)
        if _digest(backup) != info['files'][str(path)]:
            raise RuntimeError('全文更换备份校验失败，未修改产物')
    if info['changed']:
        originals = target / 'original'
        originals.mkdir()
        for path in generated:
            if path in handles:
                if os.name != 'nt':
                    from library_platform import mac_move_locked
                    handles[path].seek(0)
                    if hashlib.file_digest(handles[path], 'sha256').hexdigest() != info['files'][str(path)]:
                        raise RuntimeError('归档前产物发生变化；未继续处理')
                    mac_move_locked(handles[path], path, originals / path.name)
                else:
                    _move_open(handles[path], originals / path.name)
    for path, updated in updates.items():
        stream = handles[path]
        if os.name != 'nt':
            stream.seek(0)
            if hashlib.file_digest(stream, 'sha256').hexdigest() != info['files'][str(path)]:
                raise RuntimeError('重新关联前文件发生变化；未继续处理')
        stream.seek(0)
        stream.write(updated)
        stream.truncate()
        stream.flush()
        stream.seek(0)
        owned[str(path)] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'backup': str(target), 'reanchored': owned, 'force_parse': info['changed']}
