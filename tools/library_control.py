# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
PAUSED_EXIT = 75

def _sync():
    import library_sync
    return library_sync

def _read(path):
    from library_resume import read_json
    return read_json(path)

def _root(run_id):
    from library_resume import safe_path
    s = _sync()
    s.progress._run_id(run_id)
    return safe_path(s.RUNS, run_id)

def held_items(runs):
    held = {}
    for path in sorted(Path(runs).glob('*/state.json')):
        state = _read(path)
        if state.get('control_schema') != 1:
            continue
        leaf, seen = (path.parent, {path.parent.name})
        descendant_done = False
        while (leaf / 'resume_dispatch.json').is_file():
            child_id = _read(leaf / 'resume_dispatch.json').get('run_id')
            _sync().progress._run_id(child_id)
            if child_id in seen:
                raise RuntimeError('恢复历史存在循环；未重新准备附件')
            seen.add(child_id)
            leaf = Path(runs) / child_id
            if not (leaf / 'state.json').is_file():
                break
            child_state = _read(leaf / 'state.json')
            if (child_state.get('status') == 'completed' or (child_state.get('status') == 'partial' and child_state.get('workflow_complete'))) and child_state.get('monitor_done'):
                descendant_done = True
                break
        if descendant_done:
            continue
        if state.get('status') == 'paused' or (state.get('worker_resume_of') and state.get('status') == 'blocked'):
            for key in state.get('requested', []):
                held[key] = state['id']
    return held

def finalize_barrier(state, run, checkpoint):
    s = _sync()
    with s.launch_lock():
        if (run / 'pause_request.json').is_file():
            return False
        state['finalize_phase'] = 'snapshot'
        checkpoint()
        return True

def request_pause(run_id):
    s = _sync()
    root = _root(run_id)
    with s.launch_lock():
        state = _read(root / 'state.json')
        if state.get('id') != run_id:
            raise RuntimeError('任务状态与目录不匹配')
        owner = s.active_controller()
        if state.get('status') == 'paused':
            return {'run_id': run_id, 'status': 'paused', 'message': '该任务已经暂停。'}
        if not owner or owner['id'] != run_id or state.get('control_schema') != 1:
            raise RuntimeError('任务未运行或不支持安全暂停；没有终止任何进程')
        if state.get('finalize_phase') or state.get('status') != 'running':
            raise RuntimeError('已进入索引/Git收尾；请等待完成，不在提交中间中断')
        path = root / 'pause_request.json'
        if not path.exists():
            s.save_json(path, {'schema': 1, 'run_id': run_id, 'requested': dt.datetime.now().isoformat(), 'intent': 'user_requested_pause_at_step_boundary'})
    return {'run_id': run_id, 'status': 'pause_requested', 'message': '暂停请求已保存；等待当前步骤完成及工作锁释放后，状态会变为已暂停。'}

def _attachment_set(snapshot, key):
    return sorted(((x['key'], x['data'].get('filename', ''), x['data'].get('contentType', '')) for x in snapshot['items'] if x['data'].get('parentItem') == key and x['data'].get('itemType') == 'attachment' and (not x['data'].get('deleted'))))

def seal_pause(state, run):
    s = _sync()
    from library_resume import expected_note_hash
    s.assert_idle()
    if state.get('finalize_phase') or not state.get('baseline'):
        raise RuntimeError('不在可暂停的步骤边界')
    snapshot = _read(run / 'snapshot.json')
    papers = {}
    for paper in state['papers']:
        if paper.get('exit_code') not in (0, PAUSED_EXIT):
            raise RuntimeError('worker 失败不是已暂停，需检查日志')
        files = s.paper_fingerprint(paper)
        if paper.get('qa', {}).get('ok'):
            folder = (s.LIBRARY / 'papers' / paper['citekey']).resolve()
            primary = Path(paper['primary_pdf']).resolve()
            approved = {name: digest for name, digest in state.get('qa_frozen', {}).items() if Path(name).resolve().is_relative_to(folder) or Path(name).resolve() == primary}
            if files != approved:
                raise RuntimeError('已验收产物在暂停前变化，保留用户修改并停止')
        s.assert_hashes({paper['primary_pdf']: paper['source_sha256']}, 'Paused primary')
        before = state['baseline']['before']
        note = str(s.LIBRARY / 'papers' / paper['citekey'] / (paper['citekey'] + '_Note.md'))
        expected_note = expected_note_hash(paper, before, state.get('qa_frozen', {}))
        if note in before and files[note] != expected_note:
            raise RuntimeError('Existing Note changed during run; refusing pause receipt')
        papers[paper['key']] = {'files': files, 'attachments': _attachment_set(snapshot, paper['key'])}
    s.save_json(run / 'pause_receipt.json', {'schema': 1, 'run_id': state['id'], 'snapshot_sha256': s.sha256(run / 'snapshot.json'), 'papers': papers})
    state.update(status='paused', phase='paused', pause_reason='user_requested', pause_checkpoint=True)

def remaining_steps(paper):
    s = _sync()
    from library_resume import safe_path
    from sync import front_matter
    folder = safe_path(s.LIBRARY, 'papers/' + paper['citekey'])
    ck = paper['citekey']
    full = [folder / f'{ck}_{suffix}.md' for suffix in ('EN', 'MD') if (folder / f'{ck}_{suffix}.md').is_file()]
    if len(full) > 1:
        raise RuntimeError('同时存在 EN/MD，不能自动判断恢复来源')
    steps = []
    if not full:
        steps.extend(('folders', 'md', 'qc'))
    else:
        front = front_matter(full[0].read_text(encoding='utf-8-sig'))[0]
        if front.get('citekey') != ck or front.get('zotero_key') != paper['key']:
            raise RuntimeError('现有全文身份与暂停记录不一致')
        if not front.get('qc'):
            steps.append('qc')
    if s.paper_translation_required(paper) and (not (full and full[0].name.endswith('_MD.md'))) and (not (folder / f'{ck}_ZH.md').exists()):
        steps.append('zh')
    if not (folder / f'{ck}_Note.md').exists():
        steps.append('note')
    return steps

def prepare_resume(client, run_id, attempt=None):
    s = _sync()
    from library_resume import expected_note_hash, safe_path, verify_identity
    root = _root(run_id)
    state = _read(root / 'state.json')
    if state.get('id') != run_id or state.get('control_schema') != 1 or state.get('status') != 'paused' or (state.get('monitor_done') is not True) or state.get('finalize_phase'):
        raise RuntimeError('仅支持有完整安全暂停记录的任务；历史中断不能冒充安全暂停')
    if state.get('controller') and s.progress.owner_matches(state['controller']):
        raise RuntimeError('原控制器尚未退出，请稍后继续')
    dispatch_file = root / 'resume_dispatch.json'
    if dispatch_file.exists() and _read(dispatch_file).get('run_id') != attempt:
        raise RuntimeError('该暂停记录已提交恢复，请查看其后续任务，不重复启动')
    s.assert_idle()
    s.assert_unstaged()
    receipt = _read(root / 'pause_receipt.json')
    if receipt.get('schema') != 1 or receipt.get('run_id') != run_id:
        raise RuntimeError('暂停凭据无效')
    guards = s.fingerprint([root / 'state.json', root / 'pause_receipt.json', root / 'snapshot.json'])
    if guards.get(str(root / 'snapshot.json')) != receipt.get('snapshot_sha256'):
        raise RuntimeError('暂停快照已改变')
    old_snapshot = _read(root / 'snapshot.json')
    papers = copy.deepcopy(state['papers'])
    keys = {p['key'] for p in papers}
    if len(keys) != len(papers) or keys != set(state['requested']) or keys != set(receipt['papers']):
        raise RuntimeError('暂停条目范围不完整或有重复')
    baseline = state['baseline']
    owned = [safe_path(s.LIBRARY, f'papers/{p['citekey']}/{p['citekey']}_{suffix}.md') for p in papers for suffix in ('EN', 'MD', 'ZH', 'Note')]
    if baseline.get('schema') != 1 or set(baseline['owned']) != {str(p) for p in owned}:
        raise RuntimeError('暂停所有权基线不匹配')
    if not set(baseline['before']) <= set(baseline['owned']):
        raise RuntimeError('暂停所有权路径无效')
    s.assert_indexes(baseline['before_index'])
    items, frozen, protected, work = ([], {}, {}, {})
    for paper in papers:
        if paper.get('status') != 'applied' or paper.get('blocked') or paper.get('redo_required'):
            raise RuntimeError('附件准备尚未完成，不能跳过准备阶段恢复')
        expected = receipt['papers'][paper['key']]['files']
        current = s.paper_fingerprint(paper)
        if current != expected:
            raise RuntimeError(f'{paper['citekey']} 的暂停产物/正本有变化，保留改动并停止')
        s.assert_hashes({paper['primary_pdf']: paper['source_sha256']}, 'Resume primary')
        family = client.parent_children(paper['key'])
        family_items = [family['parent'], *family['children']]
        verify_identity({'items': family_items}, [paper])
        attachments = [list(x) for x in _attachment_set({'items': family_items}, paper['key'])]
        if attachments != receipt['papers'][paper['key']]['attachments']:
            raise RuntimeError('附件记录或角色已变化；恢复不会生成新附件')
        if Path(client.file_path(paper['primary_key'])).resolve() != Path(paper['primary_pdf']).resolve():
            raise RuntimeError('正本路径已改变')
        note = str(safe_path(s.LIBRARY, f'papers/{paper['citekey']}/{paper['citekey']}_Note.md'))
        expected_note = expected_note_hash(paper, baseline['before'], state.get('qa_frozen', {}))
        if note in baseline['before'] and current[note] != expected_note:
            raise RuntimeError('Existing Note differs from its source/pause receipt')
        items.extend(family_items)
        steps = remaining_steps(paper)
        work[paper['key']] = steps
        may_change = {str(s.LIBRARY / 'papers' / paper['citekey'] / f'{paper['citekey']}_{k}.md') for k in ('EN', 'MD')} if 'qc' in steps else set()
        protected.update({name: digest for name, digest in expected.items() if digest is not None and name not in may_change})
        if not steps:
            paper['exit_code'] = 0
            frozen.update(s.validate_completed_paper(paper))
            if not paper['qa']['ok']:
                raise RuntimeError('已完成产物复验失败；不自动重解析或重译')
    if state.get('model_config'):
        from model_api import validate_config
        needed = tuple((role for role in ('qc', 'zh') if any((role in steps for steps in work.values()))))
        validate_config(state['model_config'], require_credentials=bool(needed), required_roles=needed or ('qc', 'zh'))
    dirty = s.dirty_paths(s.LIBRARY)
    if any((x.startswith('indexes/') for x in dirty)):
        raise RuntimeError('索引有用户修改，不覆盖')
    batch_changes = {p.relative_to(s.LIBRARY).as_posix() for p in owned if s.fingerprint([p]).get(str(p)) != baseline['before'].get(str(p))}
    dirty = dirty - batch_changes | set(baseline['library_dirty'])
    s.assert_hashes(guards, 'Original pause records')
    return dict(original=state, papers=papers, owned=owned, baseline=baseline, snapshot=s.live_snapshot({'time': dt.datetime.now().isoformat(), 'items': items, 'collections': old_snapshot['collections']}), guards=guards, frozen=frozen, protected=protected, steps=work, dirty=dirty, tools_dirty=s.dirty_paths(s.state_repo()))

def new_attempt():
    return dt.datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6]

def launch_resume(run_id, *, finalize_only=False):
    s = _sync()
    root = _root(run_id)
    with s.launch_lock():
        owner = s.active_controller()
        if owner:
            return {'run_id': owner['id'], 'started': False, 'joined': True}
        source = _read(root / 'state.json')
        retry_root = None
        if source.get('resume_preflight_failed') is True:
            if source.get('status') != 'blocked' or not source.get('monitor_done') or source.get('papers') or source.get('requested') or (source.get('controller') and s.progress.owner_matches(source['controller'])):
                raise RuntimeError('失败预检仍不满足安全重试条件')
            retry_root = root
            run_id = source['worker_resume_of']
            root = _root(run_id)
            if _read(root / 'resume_dispatch.json').get('run_id') != retry_root.name:
                dispatch = _read(root / 'resume_dispatch.json')
                return {'run_id': dispatch['run_id'], 'started': False, 'already_submitted': True}
            source = _read(root / 'state.json')
        path = root / 'resume_dispatch.json'
        if path.exists() and retry_root is None:
            old = _read(path)
            return {'run_id': old['run_id'], 'started': False, 'already_submitted': True, 'resume_of': run_id}
        if finalize_only:
            view = s.progress.snapshot(s.RUNS, run_id, s.LIBRARY)
            if not view.get('can_resume_finalize'):
                raise RuntimeError('此任务不在可核实的仅收尾恢复范围')
        elif source.get('control_schema') != 1 or source.get('status') != 'paused' or (not source.get('monitor_done')):
            raise RuntimeError('此任务没有可恢复的安全暂停记录')
        s.assert_idle()
        attempt = new_attempt()
        destination = s.RUNS / attempt
        destination.mkdir(parents=True, exist_ok=False)
        s.save_json(path, {'schema': 1, 'run_id': attempt, 'source': run_id, 'source_sha256': s.sha256(root / 'state.json'), 'finalize_only': finalize_only})
        if retry_root is not None:
            s.save_json(retry_root / 'resume_dispatch.json', {'schema': 1, 'run_id': attempt, 'source': run_id})
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        command = [str(python), '-B', '-X', 'utf8', str(s.REPO / 'tools' / 'library_sync.py'), '--resume-run', run_id, '--attempt-id', attempt, '--apply']
        if finalize_only:
            command += ['--finalize-only']
        try:
            with (destination / 'controller.log').open('w', encoding='utf-8') as log:
                proc = subprocess.Popen(command, cwd=s.REPO, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if not s.register_controller(attempt, proc.pid):
                raise RuntimeError('恢复进程身份尚未确认')
        except Exception:
            s.save_json(destination / 'launch_failure.json', {'source': run_id, 'retry_automatically': False})
            raise RuntimeError('恢复进程启动未确认；保留记录，请检查，未自动重试') from None
    return {'run_id': attempt, 'started': True, 'resume_of': run_id}

def resume_run(client, run_id, *, apply=False, attempt=None, finalize_only=False):
    s = _sync()
    s.progress._run_id(run_id)
    if attempt is not None:
        s.progress._run_id(attempt)
    if finalize_only:
        from library_resume import resume_finalize
        return resume_finalize(client, run_id, apply=apply, attempt_id=attempt)
    if not apply:
        prepared = prepare_resume(client, run_id)
        print(json.dumps({'run_id': run_id, 'steps': prepared['steps'], 'dry_run': True}, ensure_ascii=False))
        return 0
    if attempt is None:
        result = launch_resume(run_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    root = _root(run_id)
    dispatch = _read(root / 'resume_dispatch.json')
    if dispatch.get('run_id') != attempt or dispatch.get('source_sha256') != s.sha256(root / 'state.json'):
        raise RuntimeError('恢复派发与原暂停记录不一致')
    if (s.RUNS / attempt / 'state.json').exists():
        raise RuntimeError('该恢复 attempt 已执行，禁止覆盖原记录')
    with s.controller_lock(attempt):
        run = s.RUNS / attempt
        if (run / 'state.json').exists():
            raise RuntimeError('该恢复 attempt 已执行，禁止覆盖原记录')
        report = s.REPORTS / f'library_sync_{attempt}.md'
        state = dict(id=attempt, worker_resume_of=run_id, control_schema=1, started=dt.datetime.now().isoformat(), status='running', phase='preflight', papers=[], requested=[], warnings=[], deferred=[], chunk_parallel=1, controller=s.progress.process_identity(os.getpid()), monitor_done=False, report=str(report))
        commit, tools_dirty, ledger = (False, set(), None)
        checkpoint = lambda: s.progress_checkpoint(state, run)
        try:
            checkpoint()
            p = prepare_resume(client, run_id, attempt)
            old = p['original']
            commit = old.get('commit', True)
            tools_dirty = p['tools_dirty']
            state.update(papers=p['papers'], requested=old['requested'], translate=old.get('translate', True), chunk_parallel=old['chunk_parallel'], workers=old.get('workers', 1), commit=commit, baseline=copy.deepcopy(p['baseline']), planned_papers=old.get('planned_papers', []), model_config=old.get('model_config'), deferred=old.get('deferred', []), qa_frozen=p['frozen'], resume_steps=p['steps'], phase='workers')
            state['baseline']['library_dirty'] = sorted(p['dirty'])
            state['baseline']['tools_dirty'] = sorted(tools_dirty)
            s.save_json(run / 'snapshot.json', p['snapshot'])
            if (s.RUNS / 'discovery.json').exists():
                from library_discovery import load
                ledger = load(s.RUNS)
            env = os.environ.copy()
            env.update(PYTHONUTF8='1', QC_PARALLEL=str(state['chunk_parallel']), ZH_PARALLEL=str(state['chunk_parallel']), GLM_MAX_CONCURRENCY='2', LIBRARY_SYNC_PAUSE_FILE=str(run / 'pause_request.json'))
            model_args = ['--model', 'bigmodel/glm-5.3-flash']
            if state.get('model_config'):
                s.save_json(run / 'model_config.json', state['model_config'])
                model_args = ['--api-config', str(run / 'model_config.json')]
            checkpoint()
            for paper in state['papers']:
                s.assert_hashes(p['protected'], 'Completed content')
                steps = p['steps'][paper['key']]
                if (run / 'pause_request.json').is_file():
                    if steps:
                        paper['exit_code'] = PAUSED_EXIT
                    continue
                if steps:
                    s.assert_hashes(p['protected'], 'Completed content')
                    logfile = run / (paper['key'] + '.log')
                    paper.pop('qa', None)
                    paper['progress_phase'] = steps[0]
                    paper['log'] = str(logfile)
                    checkpoint()
                    args = ['--only', paper['citekey'], '--snapshot', str(run / 'snapshot.json'), '--steps', ','.join(steps), '--apply', *model_args, '--worker-id', 'resume_' + paper['key']]
                    if paper.get('source_refresh', {}).get('force_parse'):
                        args += ['--fresh-source', '--retry-failed']
                    paper['exit_code'] = s.run_sync(args, logfile, env)
                    text = logfile.read_text(encoding='utf-8', errors='replace')
                    paper['qc_log'] = paper.get('qc_log', []) + [line for line in text.splitlines() if re.search('rejected|reject |chunk dropped|qc done|accepted|SUMMARY|!!', line, re.I)]
                    s.assert_hashes(p['protected'], 'Completed content')
                    if paper['exit_code'] == PAUSED_EXIT:
                        paper['progress_phase'] = 'paused'
                        checkpoint()
                        continue
                    if paper['exit_code'] != 0:
                        raise RuntimeError('恢复 worker 失败；未自动重试，请查看本篇日志')
                paper['exit_code'] = 0
                with s.freeze_files(p['protected']):
                    s.assert_hashes(p['protected'], 'Completed content')
                    files = s.validate_completed_paper(paper)
                    s.assert_hashes(p['protected'], 'Completed content')
                    if not steps and any((p['frozen'].get(k) != v for k, v in files.items())):
                        raise RuntimeError('已完成文献变化，不能更新恢复基线')
                if not paper['qa']['ok']:
                    raise RuntimeError('恢复产物验收未通过；不绕过质检、不重译已有内容')
                state['qa_frozen'].update(files)
                paper['progress_phase'] = 'validated'
                checkpoint()
            if not finalize_barrier(state, run, checkpoint):
                seal_pause(state, run)
                return s.finish(state, run, report, commit=False, tools_dirty=tools_dirty, discovery_ledger=ledger)
            source_map = {x['key']: x for x in p['snapshot']['items']}
            s.assert_hashes(p['guards'], 'Original pause records')

            def verify_live(snapshot):
                from library_resume import verify_identity
                verify_identity(snapshot, state['papers'])
                for paper in state['papers']:
                    if _attachment_set(snapshot, paper['key']) != _attachment_set(p['snapshot'], paper['key']):
                        raise RuntimeError('恢复期间附件记录已改变，未发布索引')
                    if Path(client.file_path(paper['primary_key'])).resolve() != Path(paper['primary_pdf']).resolve():
                        raise RuntimeError('恢复期间正本路径变化')
            s.finalize(client, state, run, frozen=state['qa_frozen'], before_index=p['baseline']['before_index'], before=p['baseline']['before'], owned=p['owned'], source_map=source_map, dirty=p['dirty'], commit=commit, env=env, checkpoint=checkpoint, verify_snapshot=verify_live, extra_guard=lambda: s.assert_hashes(p['guards'], 'Original pause records'))
        except Exception as error:
            state.update(status='blocked', error=f'{type(error).__name__}: {error}')
            if not state['papers'] and (not state['requested']):
                state['resume_preflight_failed'] = True
        return s.finish(state, run, report, commit=commit, tools_dirty=tools_dirty, discovery_ledger=ledger)

def resume_published(client, run_id, *, apply=False, attempt=None):
    s = _sync()
    from library_resume import safe_path, verify_identity
    root = _root(run_id)

    def prepare():
        original = _read(root / 'state.json')
        if original.get('id') != run_id or original.get('control_schema') != 1 or original.get('status') not in ('blocked', 'attention') or (not original.get('monitor_done')) or (not original.get('baseline')) or (not original.get('published_indexes')) or (original.get('finalize_phase') not in ('indexes_published', 'library_commit_started', 'finalized')) or (not isinstance(original.get('finalize', {}).get('commit_paths'), list)):
            raise RuntimeError('缺少完整索引发布/提交意图凭据，不能自动恢复')
        if original.get('controller') and s.progress.owner_matches(original['controller']):
            raise RuntimeError('原控制器仍在运行')
        dispatched = root / 'resume_dispatch.json'
        if dispatched.exists() and _read(dispatched).get('run_id') != attempt:
            raise RuntimeError('已有收尾恢复派发，不重复执行')
        s.assert_idle()
        before_hash = s.sha256(root / 'state.json')
        papers = copy.deepcopy(original['papers'])
        if not papers or {p['key'] for p in papers} != set(original['requested']):
            raise RuntimeError('收尾条目范围不完整')
        frozen = {}
        source_map = {}
        for paper in papers:
            if paper.get('exit_code') != 0 or paper.get('qa', {}).get('ok') is not True:
                raise RuntimeError('仍有未通过验收的文献，不能只补 Git')
            family = client.parent_children(paper['key'])
            source_map[paper['key']] = family['parent']
            verify_identity({'items': [family['parent'], *family['children']]}, [paper])
            if Path(client.file_path(paper['primary_key'])).resolve() != Path(paper['primary_pdf']).resolve():
                raise RuntimeError('正本路径变化')
            frozen.update(s.validate_completed_paper(paper))
            if not paper['qa']['ok']:
                raise RuntimeError('收尾复验失败')
        if frozen != original.get('qa_frozen'):
            raise RuntimeError('已验收产物在索引发布后变化，保留用户修改并停止')
        published = original['published_indexes']
        s.assert_indexes(published)
        allowed = {Path(name).relative_to(s.LIBRARY).as_posix() for name in original['baseline']['owned']}
        allowed |= {Path(name).relative_to(s.LIBRARY).as_posix() for name in published}
        selected = original['finalize']['commit_paths']
        if len(selected) != len(set(selected)) or not set(selected) <= allowed:
            raise RuntimeError('收尾提交清单超出本批所有权')
        for name in selected:
            safe_path(s.LIBRARY, name)
        baseline = original['baseline']
        protected = set(baseline['library_dirty'])
        claimed = set(original['finalize'].get('recovered_paths', []))
        if not claimed <= protected.intersection(selected):
            raise RuntimeError('历史生成文件超出原收尾清单')
        current_dirty = s.dirty_paths(s.LIBRARY)
        pending = claimed & current_dirty
        if pending:
            recovered = s.previous_generated_paths(original, baseline['before'], protected, frozen, source_map)
            if not pending <= recovered:
                raise RuntimeError('原有文件缺少生成记录或已变化；停止恢复提交')
            protected -= pending
        dirty = current_dirty - set(selected) | protected
        verify_owned_staging(selected, dirty)
        s.assert_hashes({str(root / 'state.json'): before_hash}, 'Original finalize receipt')
        return (original, papers, frozen, published, selected, dirty, before_hash)
    if not apply:
        original, papers, *_ = prepare()
        print(f'仅预览：{len(papers)} 篇与已发布索引哈希一致；只恢复 Git 与报告收尾，不发送桌面通知。')
        return 0
    if attempt is None:
        print(json.dumps(launch_resume(run_id, finalize_only=True), ensure_ascii=False))
        return 0
    s.progress._run_id(attempt)
    if (s.RUNS / attempt / 'state.json').exists():
        raise RuntimeError('该恢复 attempt 已执行，禁止覆盖原记录')
    with s.controller_lock(attempt):
        run = s.RUNS / attempt
        if (run / 'state.json').exists():
            raise RuntimeError('该恢复 attempt 已执行，禁止覆盖原记录')
        report = s.REPORTS / f'library_sync_{attempt}.md'
        state = dict(id=attempt, resume_of=run_id, control_schema=1, started=dt.datetime.now().isoformat(), status='running', phase='preflight', papers=[], requested=[], warnings=[], deferred=[], chunk_parallel=1, controller=s.progress.process_identity(os.getpid()), monitor_done=False, report=str(report))
        tools_dirty, ledger = (s.dirty_paths(s.state_repo()), None)
        try:
            s.progress_checkpoint(state, run)
            original, papers, frozen, published, selected, dirty, before_hash = prepare()
            dispatch = _read(root / 'resume_dispatch.json')
            if dispatch.get('source_sha256') != before_hash:
                raise RuntimeError('恢复派发后原收尾记录变化')
            for name in ('baseline', 'translate', 'model_config', 'chunk_parallel', 'requested', 'deferred', 'commit', 'finalize'):
                if name in original:
                    state[name] = copy.deepcopy(original[name])
            state.update(papers=papers, qa_frozen=frozen, published_indexes=published, finalize_phase='indexes_published')

            def guard():
                s.assert_idle()
                s.assert_hashes(frozen, 'QA inputs')
                s.assert_indexes(published)
                s.assert_hashes({str(root / 'state.json'): before_hash}, 'Original finalize receipt')
            with s.freeze_files({**frozen, **published}):
                guard()
                state['finalize_phase'] = 'library_commit_started'
                s.progress_checkpoint(state, run)
                if original.get('commit', True):
                    state['library_commit'] = commit_after_publication(selected, '恢复文献收尾 ' + run_id, dirty, expected_hashes={**frozen, **published}, guard=guard)
                    if not state['library_commit']:
                        state['library_commit'] = original.get('library_commit')
                state.update(finalize_phase='finalized', status='completed')
                guard()
            if (s.RUNS / 'discovery.json').exists():
                from library_discovery import load
                ledger = load(s.RUNS)
        except Exception as error:
            state.update(status='blocked', error=f'{type(error).__name__}: {error}')
        return s.finish(state, run, report, commit=state.get('commit', True), tools_dirty=tools_dirty, discovery_ledger=ledger)

def verify_owned_staging(paths, baseline_dirty):
    s = _sync()
    if s.git(s.state_repo(), 'diff', '--cached', '--name-only', '-z'):
        raise RuntimeError('工具仓库有预存暂存内容；保留并停止自动收尾')
    staged = set(filter(None, s.git(s.LIBRARY, 'diff', '--cached', '--name-only', '-z').split('\x00')))
    if not staged:
        return staged
    allowed = set(paths) - set(baseline_dirty)
    if not staged <= allowed:
        raise RuntimeError('Git 暂存包含其他用户内容，未修改暂存区')
    expected = allowed & s.dirty_paths(s.LIBRARY)
    if staged != expected:
        raise RuntimeError('Git 暂存集合与本批待提交文件不一致；保留用户取消暂存的选择')
    if s.git(s.LIBRARY, 'diff', '--name-only', '-z', '--', *sorted(staged)):
        raise RuntimeError('原提交暂存内容与已验收文件不一致，未改动暂存区')
    return staged

def commit_after_publication(paths, message, baseline_dirty, *, expected_hashes, guard):
    s = _sync()
    staged = verify_owned_staging(paths, baseline_dirty)
    if not staged:
        return s.scoped_commit(s.LIBRARY, paths, message, baseline_dirty, expected_hashes=expected_hashes, guard=guard)
    allowed = sorted(staged)
    approved = {str(s.LIBRARY / name): expected_hashes[str(s.LIBRARY / name)] for name in allowed}
    with s.freeze_files(approved):
        guard()
        verify_owned_staging(paths, baseline_dirty)
        s.git(s.LIBRARY, 'commit', '--only', '-m', message, '--', *allowed)
    return s.git(s.LIBRARY, 'rev-parse', '--short', 'HEAD').strip()
