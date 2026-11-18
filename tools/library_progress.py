# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import ctypes
from ctypes import wintypes
import datetime as dt
import errno
import json
import math
import ntpath
import os
from pathlib import Path
import re
import sys
import time
RUN_ID = re.compile('[0-9]{8}_[0-9]{6}_[0-9a-f]{6}\\Z')
TERMINAL = frozenset({'completed', 'partial', 'blocked', 'attention', 'failed', 'interrupted', 'cancelled', 'canceled', 'paused'})
LABELS = {'pending': '等待/准备', 'running': '运行中', 'completed': '已完成', 'partial': '部分完成，需要处理', 'blocked': '受阻，需要处理', 'attention': '需要处理', 'failed': '失败，需要处理', 'interrupted': '已中断，需要处理', 'cancelled': '已取消', 'canceled': '已取消', 'finalizing': '收尾中，等待完成确认', 'preflight': '预检', 'intake': '附件与元数据准备', 'worker_snapshot': '准备文献快照', 'workers': '文献处理中', 'md': '全文解析', 'qc': '全文质检', 'zh': '中文翻译', 'qa': '产物验收', 'validated': '验收通过', 'snapshot': '准备全库索引快照', 'index_stage': '构建并校验索引', 'publishing_indexes': '发布索引', 'indexes_published': '索引已发布', 'library_commit_started': '提交 Library Git', 'finalized': '等待报告与收尾记录', 'report': '写入报告', 'tools_commit': '提交报告 Git', 'tools_commit_started': '提交报告 Git', 'notify': '记录面板结果', 'notification': '记录面板结果', 'done': '收尾完成', 'pause_requested': '正在安全暂停，等待当前步骤完成', 'paused': '已暂停'}

class ProgressError(RuntimeError):
    pass

def _kernel32():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel

def process_identity(pid):
    if type(pid) is not int or not 0 < pid <= 4294967295:
        raise ValueError('PID 必须为正的 DWORD 整数')
    if os.name != 'nt':
        from library_platform import IS_MAC, mac_identity
        if not IS_MAC:
            raise NotImplementedError('Unsupported operating system')
        return mac_identity(pid)
    kernel = _kernel32()
    handle = kernel.OpenProcess(4096 | 1048576, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return None
        raise ctypes.WinError(error)
    try:

        def exited():
            result = kernel.WaitForSingleObject(handle, 0)
            if result == 0:
                return True
            if result == 258:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        if exited():
            return None
        created, ended, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(system), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if exited():
            return None
        if not ntpath.isabs(name.value):
            raise ProgressError('进程可执行路径不是绝对路径')
        return {'pid': pid, 'created': str(created.dwHighDateTime << 32 | created.dwLowDateTime), 'exe': name.value}
    finally:
        kernel.CloseHandle(handle)

def _validate_owner(owner):
    if not isinstance(owner, dict) or type(owner.get('pid')) is not int or (not 0 < owner['pid'] <= 4294967295) or (not isinstance(owner.get('created'), str)) or (not owner['created']) or (not isinstance(owner.get('exe'), str)) or (not (ntpath.isabs(owner['exe']) or os.path.isabs(owner['exe']))) or any((ord(c) < 32 for c in owner['exe'])):
        raise ProgressError('控制器身份记录损坏；需要检查')
    return owner

def owner_matches(owner):
    _validate_owner(owner)
    current = process_identity(owner['pid'])
    return bool(current and current['pid'] == owner['pid'] and (current['created'] == owner['created']) and (os.path.normpath(current['exe']) == os.path.normpath(owner['exe']) if os.name != 'nt' else ntpath.normcase(ntpath.normpath(current['exe'])) == ntpath.normcase(ntpath.normpath(owner['exe']))))

def _run_id(value):
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise ProgressError('任务 id 格式无效')
    return value

def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result

def _read_json(path):
    for attempt in range(3):
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ValueError('expected object')
            return value
        except FileNotFoundError:
            if attempt == 2:
                return None
        except (ValueError, UnicodeError):
            if attempt == 2:
                raise ProgressError(f'{path.name} 未能完整读取或格式损坏；需要检查') from None
        except OSError as error:
            sharing = getattr(error, 'winerror', None) in (5, 32, 33) or (isinstance(error, PermissionError) and error.errno == errno.EACCES)
            if not sharing or attempt == 2:
                raise ProgressError(f'{path.name} 暂不可读；需要检查权限或文件占用') from None
        time.sleep(0.03)

def _active_record(runs):
    record = _read_json(Path(runs) / 'active.json')
    if record is not None:
        _run_id(record.get('id'))
        _validate_owner(record)
    return record

def active_run(runs):
    record = _active_record(runs)
    if record is None or not owner_matches(record):
        return None
    state = _read_json(Path(runs) / record['id'] / 'state.json')
    if state is not None:
        if state.get('id') != record['id']:
            raise ProgressError('任务状态 id 与登记记录不符')
        if state.get('monitor_done') is True:
            return None
    return record

def _clean(value, limit=240):
    text = str(value or '')
    text = re.sub('(?i)bearer\\s+\\S+', 'Bearer [已隐藏]', text)
    text = re.sub('(?i)((?:api[_-]?key|token|secret|password|authorization)\\s*[=:]\\s*)[^\\s,;]+', '\\1[已隐藏]', text)
    text = ' '.join(''.join((c if c.isprintable() else ' ' for c in text)).split())
    return text[:limit]

def _timestamp(value):
    if isinstance(value, dt.datetime):
        return value.timestamp()
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None

def _elapsed(started, now):
    value = _timestamp(started)
    return max(0.0, now - value) if value is not None else None

def _papers(state):
    merged = []
    for field in ('planned_papers', 'papers'):
        entries = state.get(field, [])
        if not isinstance(entries, list):
            raise ProgressError('文献状态列表损坏')
        for entry in entries:
            if not isinstance(entry, dict) or not (entry.get('key') or entry.get('citekey')):
                raise ProgressError('文献身份记录损坏')
            existing = next((p for p in merged if entry.get('key') and p.get('key') == entry['key'] or (not entry.get('key') and entry.get('citekey') and (p.get('citekey') == entry['citekey']))), None)
            values = {k: entry[k] for k in ('key', 'citekey', 'title') if k in entry} if field == 'planned_papers' else dict(entry)
            if existing is None:
                merged.append(values)
            else:
                existing.update(values)
    return merged

def _paper_view(paper, state, library, started, now, terminal, needs_action=None):
    qa = paper.get('qa')
    if qa is not None and (not isinstance(qa, dict)):
        raise ProgressError('文献 QA 状态损坏')
    accepted = isinstance(qa, dict) and qa.get('ok') is True and (type(paper.get('exit_code')) is int) and (paper['exit_code'] == 0)
    phase, source, stamp, note = ('pending', 'none', None, '')
    pending_reasons = (needs_action or {}).get(paper.get('key'), {}).get('reasons')
    if pending_reasons and (not accepted):
        phase, source = ('attention', 'needs_action')
        note = '已转入待处理：' + '；'.join((str(x) for x in pending_reasons[:4]))
    if accepted:
        phase, source = ('validated', 'qa')
    elif paper.get('exit_code') == 75 or paper.get('progress_phase') == 'paused':
        phase, source, note = ('paused', 'state', '已保留完成步骤；继续时只补缺失步骤。')
    elif paper.get('blocked') or paper.get('status') in ('blocked', 'failed') or (isinstance(qa, dict) and qa.get('ok') is False) or (paper.get('exit_code') is not None and paper['exit_code'] != 0) or (paper.get('progress_phase') == 'failed'):
        phase, source = ('failed', 'state')
        note = '本篇尚未通过验收；请查看报告。'
        if isinstance(qa, dict) and qa.get('ok') is False:
            reasons = {'quote_coverage': '中英文对照覆盖不一致', 'quote_source': '英文对照与原文不一致', 'translation_marker': '译文残留内部编号', 'translation_empty': '译文或子图标签缺失', 'structure_coverage': '中英文段落结构不一致', 'image_coverage': '图片数量或引用不一致'}
            codes = list(dict.fromkeys((e.get('code') for e in qa.get('errors', []) if isinstance(e, dict))))
            labels = [reasons[code] for code in codes if code in reasons]
            if labels:
                note = '验收未通过，' + '、'.join(labels) + '。请查看报告。'
        for line in reversed(paper.get('qc_log') or []):
            if not isinstance(line, str):
                continue
            match = re.fullmatch('!! (qc|translation) failed [^:\\r\\n]+: incomplete_completion(?: \\(finish_reason=(length|content_filter|tool_calls|function_call|aborted|insufficient_system_resource|missing|unknown)\\))?', line)
            if match:
                step = '质检' if match[1] == 'qc' else '翻译'
                detail = f'（finish_reason={match[2]}）' if match[2] else '（旧日志未记录结束原因）'
                note = f'{step}回复未正常结束{detail}，该步未通过。'
                break
    else:
        explicit = paper.get('progress_phase') or paper.get('phase')
        if explicit in ('md', 'qc', 'zh', 'qa', 'intake', 'preflight'):
            phase, source = (explicit, 'state')
            stamp = paper.get('phase_started') or paper.get('progress_phase_started')
        elif state.get('phase') == 'intake' and state.get('current_key') == paper.get('key'):
            phase, source, stamp = ('intake', 'state', state.get('phase_started'))
        ck = paper.get('citekey')
        valid_ck = isinstance(ck, str) and bool(ck) and (ck not in ('.', '..')) and (not ck.endswith(('.', ' '))) and (not re.search('[<>:"/\\\\|?*\\x00-\\x1f]', ck)) and (not re.fullmatch('(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\\..*)?', ck))
        if ck and (not valid_ck):
            note = '等待生成最终 citekey；暂不读取文献目录。'
        if valid_ck:
            root = Path(library) / 'papers'
            folder = root / ck
            if not folder.resolve().is_relative_to(root.resolve()):
                raise ProgressError('文献路径越出 papers 目录')
            locks = []
            for step in ('md', 'qc', 'zh'):
                try:
                    mtime = (folder / f'.sync_{step}.lock').stat().st_mtime
                except FileNotFoundError:
                    continue
                except OSError:
                    raise ProgressError('阶段锁不可读；无法可靠判断进度') from None
                if (started is None or mtime >= started) and mtime <= now:
                    locks.append((mtime, step))
            if len(locks) == 1 and explicit not in ('qa', 'failed'):
                stamp, phase = locks[0]
                source = 'lock_mtime'
            elif len(locks) > 1:
                note = '发现多个阶段锁，阶段归属待确认。'
                if source != 'state':
                    phase, stamp = ('pending', None)
        if paper.get('exit_code') == 0 and phase == 'pending':
            note = 'worker 已退出，等待 QA 验收记录。'
    if terminal and (not accepted) and (not (isinstance(qa, dict) and qa.get('ok') is False)):
        note = '任务已结束或中断，本篇尚未验收。' + note
    return {'key': _clean(paper.get('key')), 'citekey': _clean(paper.get('citekey')), 'title': _clean(paper.get('title')), 'accepted': accepted, 'phase': phase, 'phase_label': LABELS[phase], 'elapsed_seconds': _elapsed(stamp, now), 'phase_elapsed_seconds': _elapsed(stamp, now), 'source': source, 'note': note}

def _finalize_view(state, terminal, status):
    phase = state.get('finalize_phase')
    indexes = '等待'
    git = '等待'
    if phase in ('snapshot', 'index_stage', 'publishing_indexes'):
        indexes = LABELS[phase]
    elif phase in ('indexes_published', 'library_commit_started', 'finalized'):
        indexes = '已发布'
    if phase == 'library_commit_started':
        git = 'Library 提交中'
    elif phase == 'finalized':
        git = 'Library 收尾已记录；等待报告与收尾确认'
    if state.get('library_commit'):
        git = 'Library 已提交；等待报告与收尾确认'
    if state.get('tools_commit'):
        git = 'Library/报告 Git 已记录；等待收尾确认'
    if state.get('commit') is False:
        git = '未请求 Git 提交'
    if terminal:
        if status == 'completed' or (status == 'partial' and state.get('workflow_complete')):
            if phase == 'finalized':
                indexes = '已完成'
                if state.get('commit') is not False:
                    receipts = [f'{label} {value}' for label, value in (('Library', state.get('library_commit')), ('报告', state.get('tools_commit'))) if isinstance(value, str) and re.fullmatch('[a-fA-F0-9]{7,64}', value)]
                    git = '已提交 ' + '、'.join(receipts) if receipts else '收尾完成（无提交哈希）'
            elif phase is None:
                indexes, git = ('历史记录未单列', '历史记录未单列')
        else:
            if indexes != '已发布':
                indexes = '未确认完成（任务需要处理）'
            if not (state.get('library_commit') or state.get('tools_commit')) and state.get('commit') is not False:
                git = '未确认完成（任务需要处理）'
    return (indexes, git)

def snapshot(runs, run_id, library, now=None):
    _run_id(run_id)
    runs = Path(runs)
    current = time.time() if now is None else _timestamp(now)
    if current is None or not math.isfinite(current):
        raise ValueError('now 必须是有限时间戳或 datetime')
    state = _read_json(runs / run_id / 'state.json')
    active = _active_record(runs)
    if state is not None and state.get('id') != run_id:
        raise ProgressError('任务状态 id 与目录不符')
    state = state or {}
    owner = state.get('controller')
    if owner is None:
        owner = _read_json(runs / run_id / 'controller.json')
        if owner is not None and owner.get('id', run_id) != run_id:
            raise ProgressError('控制器登记 id 与目录不符')
    if owner is None and active is not None and (active['id'] == run_id):
        owner = active
    alive = owner_matches(owner) if owner is not None else None
    raw_status = state.get('status', 'pending')
    if not isinstance(raw_status, str):
        raise ProgressError('任务状态字段损坏')
    if 'monitor_done' in state and type(state['monitor_done']) is not bool:
        raise ProgressError('收尾确认字段损坏')
    status, terminal, notices = (raw_status, raw_status in TERMINAL, [])
    raw_terminal = terminal
    done = state.get('monitor_done') is True
    if owner is not None and (not done):
        if alive:
            if terminal:
                status, terminal = ('finalizing', False)
                notices.append('失败已确认，正在保存报告与收尾记录；控制器退出前不可恢复或重新启动。' if state.get('error') else '终态已确认，正在保存报告与收尾记录；控制器退出前不可恢复或重新启动。')
        else:
            status, terminal = ('interrupted', True)
            notices.append('控制器已退出或身份不符；收尾未确认，需要处理。')
    elif not terminal:
        status, terminal = ('attention', True)
        notices.append('缺少可核实的运行控制器或有效终态；需要处理。')
    if raw_status not in LABELS and raw_status not in TERMINAL:
        status, terminal = ('attention', True)
        notices.append('无法识别任务状态；需要处理。')
    started = _timestamp(state.get('started'))
    if started is None:
        started = dt.datetime.strptime(run_id[:15], '%Y%m%d_%H%M%S').timestamp()
    end = _timestamp(state.get('finished')) if terminal and done else None
    if terminal and owner is None:
        end = _timestamp(state.get('finished'))
    end = min(current, end) if end is not None else current
    papers = []
    needs_rows = state.get('needs_action')
    needs_action = {row.get('key'): row for row in needs_rows if isinstance(row, dict)} if isinstance(needs_rows, list) else {}
    for paper in _papers(state):
        try:
            if paper.get('key') and (Path(runs) / run_id / f"remedy_baseline_{paper['key']}.json").is_file():
                from library_remedy import _effective_paper
                paper = _effective_paper(runs, run_id, paper, library=library)
            papers.append(_paper_view(paper, state, library, started, end, terminal, needs_action))
        except (ProgressError, OSError):
            papers.append({'key': _clean(paper.get('key')), 'citekey': _clean(paper.get('citekey')), 'title': _clean(paper.get('title')), 'accepted': False, 'phase': 'attention', 'phase_label': LABELS['attention'], 'elapsed_seconds': None, 'phase_elapsed_seconds': None, 'source': 'state_error', 'note': '本篇状态不可读；其他文献及服务连接不受影响，请查看报告。'})
            notices.append('一篇文献的状态读取需要检查。')
    accepted = sum((p['accepted'] for p in papers))
    if status == 'completed' and accepted != len(papers):
        status = 'attention'
        notices.append('完成记录与逐篇 QA/退出码不一致；需要处理。')
    if state.get('error'):
        notices.append('控制器报告错误；请查看报告中的诊断。')
    if state.get('warnings'):
        notices.append('控制器记录了注意事项；请查看报告。')
    phase = state.get('finalize_phase') or state.get('display_phase') or state.get('phase') or 'pending'
    if not isinstance(phase, str):
        raise ProgressError('阶段字段损坏')
    if terminal and phase == 'finalized' and (status == 'completed' or (status == 'partial' and state.get('workflow_complete'))):
        phase = 'done'
    indexes, git = _finalize_view(state, terminal or raw_terminal, raw_status if raw_terminal else status)
    pause_requested = bool(_read_json(runs / run_id / 'pause_request.json'))
    can_pause = bool(alive and (not terminal) and (not raw_terminal) and (state.get('control_schema') == 1) and (not state.get('finalize_phase')) and (not pause_requested))
    can_resume = bool(not alive and status == 'paused' and done and (state.get('control_schema') == 1) and (runs / run_id / 'pause_receipt.json').is_file())
    if not alive and status == 'blocked' and done and (state.get('resume_preflight_failed') is True) and (not state.get('papers')) and (not state.get('requested')) and state.get('worker_resume_of'):
        can_resume = True
        notices.append('恢复预检失败，尚未启动文献处理；排除障碍后可手动继续，原暂停记录保留。')
    can_finalize = bool(not alive and status in ('blocked', 'attention') and papers and (accepted == len(papers)) and (state.get('finalize_phase') in (None, 'preflight', 'snapshot', 'index_stage')) and (not any((state.get(k) for k in ('library_commit', 'tools_commit', 'index_backup')))))
    if not alive and status in ('blocked', 'attention') and papers and (accepted == len(papers)) and (state.get('control_schema') == 1) and state.get('published_indexes') and (state.get('finalize', {}).get('commit_paths') is not None) and (state.get('finalize_phase') in ('indexes_published', 'library_commit_started', 'finalized')):
        can_finalize = True
    can_recover_pause = bool(not alive and done and (state.get('control_schema') == 1) and (status == 'blocked') and (not state.get('finalize_phase')) and (not state.get('resume_of')) and (not state.get('worker_resume_of')) and (runs / run_id / 'pause_request.json').is_file() and (not (runs / run_id / 'pause_receipt.json').is_file()) and (not (runs / run_id / 'resume_dispatch.json').is_file()))
    if pause_requested and alive and (not terminal):
        status = 'pause_requested'
        notices.append('暂停请求已保存；当前步骤结束并释放工作锁后才可安全关机。')
    if pause_requested and (not alive) and (status == 'blocked') and (not can_recover_pause) and (not state.get('pause_receipt')):
        notices.append('该任务请求过暂停但缺少可核验的暂停凭据且不满足遗留恢复条件；可在“同步”页重新选择这些文献预演（不覆盖现有产物），或查看报告处理。')
    if status == 'paused' and alive:
        notices.append('工作步骤已停止，控制器正在释放资源；请等待退出后再关机或继续。')
    return {'id': run_id, 'status': status, 'status_text': LABELS.get(status, '需要处理'), 'terminal': terminal, 'owner_alive': alive, 'monitor_done': done, 'elapsed_seconds': _elapsed(started, end), 'phase': phase, 'phase_label': LABELS.get(phase, '等待/准备'), 'phase_elapsed_seconds': _elapsed(state.get('phase_started'), end), 'papers': papers, 'accepted': accepted, 'total': len(papers), 'index_status': indexes, 'git_status': git, 'report': _clean(state.get('report'), 1024), 'notices': notices, 'error': _clean(state.get('error'), 2048), 'started': state.get('started'), 'finished': state.get('finished'), 'pause_requested': pause_requested, 'can_pause': can_pause, 'can_resume': can_resume, 'can_resume_finalize': can_finalize, 'can_recover_pause': can_recover_pause, 'needs_action': [{'key': _clean(row.get('key')), 'citekey': _clean(row.get('citekey')), 'reasons': [_clean(x, 300) for x in row.get('reasons') or []][:6]} for row in (needs_rows if isinstance(needs_rows, list) else []) if isinstance(row, dict)], 'safe_to_shutdown': bool(status == 'paused' and done and (not alive)), 'model_config': state.get('model_config', {})}

def _duration(value):
    if value is None:
        return '未知'
    seconds = max(0, int(value))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours}小时{minutes:02d}分{seconds:02d}秒' if hours else f'{minutes}分{seconds:02d}秒'

def render(view):
    lines = [f"文献同步进度（只读）  任务 id：{_clean(view['id'])}", f"状态：{_clean(view['status_text'])}  总耗时：{_duration(view['elapsed_seconds'])}", f"阶段：{_clean(view['phase_label'])}  阶段耗时：{_duration(view['phase_elapsed_seconds'])}", f"验收：{view['accepted']}/{view['total']}"]
    for paper in view['papers']:
        lines.append(f"  {_clean(paper['citekey'] or paper['key'])}：{_clean(paper['phase_label'])} | 阶段耗时：{_duration(paper['elapsed_seconds'])}" + (' | 已验收' if paper['accepted'] else ' | 未验收'))
        if paper.get('note'):
            lines.append('    ' + _clean(paper['note']))
    lines += [f"索引：{_clean(view['index_status'])}", f"Git：{_clean(view['git_status'])}", f"报告路径：{_clean(view.get('report'), 1024) or '尚未记录'}"]
    lines.extend(('提示：' + _clean(note) for note in view.get('notices', [])))
    if view['terminal']:
        lines.append('本次任务已结束，可关闭窗口；用 -Status 或 -Watch 查看此结果。受阻任务不会自动重试。')
    else:
        lines.append('关闭此窗口或按 Ctrl+C 仅停止显示，不影响后台任务；重新打开可继续查看。')
    return '\n'.join(lines)

def watch(runs, run_id, library, interval=3, *, max_updates=None, stream=None, sleep=None, clock=None, snapshot_fn=None, stale_after=900):
    if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError('interval 必须为正数')
    if max_updates is not None and (type(max_updates) is not int or max_updates <= 0):
        raise ValueError('max_updates 必须为正整数')
    stream = sys.stdout if stream is None else stream
    sleep, clock = (sleep or time.sleep, clock or time.time)
    snapshot_fn = snapshot_fn or snapshot
    previous, changed_at, count = (None, None, 0)
    try:
        while True:
            now = clock()
            view = snapshot_fn(runs, run_id, library, now=now)
            signature = (view['status'], view['phase'], view['accepted'], view['total'], view['index_status'], view['git_status'], tuple(((p['key'], p['citekey'], p['phase'], p['accepted']) for p in view['papers'])))
            if signature != previous:
                previous, changed_at = (signature, now)
            if not view['terminal'] and now - changed_at >= stale_after:
                view = dict(view, notices=[*view['notices'], '进度证据长时间未变化，可能仍在计算或等待；请检查后台/报告，尚不能判断完成。'])
            if os.name != 'nt' and getattr(stream, 'isatty', lambda: False)():
                stream.write('\x1b[2J\x1b[H')
            stream.write(render(view) + '\n')
            stream.flush()
            count += 1
            if view['terminal']:
                return 0 if view['status'] == 'completed' else 2
            if max_updates is not None and count >= max_updates:
                stream.write('已达到测试刷新上限；后台任务尚未确认完成。\n')
                return 2
            sleep(interval)
    except KeyboardInterrupt:
        stream.write('\n已关闭进度展示；后台任务不受影响。\n')
        return 0
    except BrokenPipeError:
        return 0
    except Exception:
        stream.write('进度暂不可读取或身份无法核验，需要检查；未确认成功。关闭展示不影响后台。\n')
        stream.flush()
        return 2
