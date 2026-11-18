"""Native macOS integration acceptance in disposable directories/keychain only.

No paper, MinerU or model requests. Run only on an isolated test host/CI runner.
"""
from pathlib import Path
import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid


def command(*args, **kwargs):
    result=subprocess.run(list(args), capture_output=True, **kwargs)
    if result.returncode:
        if any(str(arg).endswith('install_macos.py') for arg in args):
            raise RuntimeError('Synthetic installer failed:\n'+result.stderr.decode(errors='replace')[-4000:])
        if '--restart-idle' in args:
            raise RuntimeError('Synthetic idle upgrade failed:\n'+result.stderr.decode(errors='replace')[-2000:])
        raise RuntimeError('Native acceptance command failed')
    return result.stdout.decode().strip()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zotero',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if sys.platform!='darwin':raise RuntimeError('Native macOS required')
    root=Path(__file__).resolve().parents[1]
    class Checks(list):
        def append(self,name):
            super().append(name)
            args.output.write_text(json.dumps({'platform':sys.platform,'checks':self,'ok':False,'phase':'running','model_requests':0},indent=2),encoding='utf-8')
    checks=Checks()
    with tempfile.TemporaryDirectory(prefix='library-sync-acceptance-') as temporary, contextlib.ExitStack() as cleanup:
        temp=Path(temporary).resolve()
        config=temp/'config.json';library=temp/'Library 中文';data=temp/'data'
        config.write_text(json.dumps({'schema_version':1,'data_dir':str(data),'library_dir':str(library),'baseline_date':'2026-01-01'}))
        os.environ['LIBRARY_SYNC_CONFIG']=str(config)
        import library_platform as platform
        import library_progress as progress
        from zotero_local import _dpapi,_atomic_bytes
        from library_plugin_bridge import write_private_manifest
        import fitz
        from mineru_client import strip_annotations,has_annotations

        import zipfile
        bundle=temp/'bundle'
        with zipfile.ZipFile(root/'release/dist/library-sync-0.2.0-beta.16-macos.zip') as archive:
            archive.extractall(bundle)
        installation=temp/'installed 应用'
        command(sys.executable,str(bundle/'app/release/install_macos.py'),'--source',str(bundle/'app'),
                '--install-dir',str(installation),'--library-dir',str(library),'--baseline-date','2026-01-01',
                '--git-user-name','Synthetic Acceptance','--git-user-email','fixture@example.invalid',
                '--init-library-git','--apply')
        receipt=json.loads((installation/'INSTALL_RECEIPT.json').read_text())
        assert receipt['runtime_ready'] is True
        checks.append('offline_installer_runtime')

        identity=progress.process_identity(os.getpid())
        assert progress.owner_matches(identity);checks.append('native_process_identity')
        fake_worker=temp/'sync.py';fake_worker.write_text('import time;time.sleep(60)',encoding='utf-8')
        worker=subprocess.Popen([sys.executable,str(fake_worker)])
        try:
            child=progress.process_identity(worker.pid);assert progress.owner_matches(child)
            assert any(row['ProcessId']==worker.pid for row in platform.mac_workers())
        finally:
            worker.terminate();worker.wait(timeout=5)
        assert not progress.owner_matches(child);checks.append('exited_process_rejected')
        checks.append('native_worker_detection')
        import sync
        step_folder=temp/'step-lock'
        assert sync.acquire(step_folder,'qc','synthetic-step')
        assert sync.lock_owned(step_folder,'qc','synthetic-step')
        assert not sync.acquire(step_folder,'qc','another-worker')
        sync.release(step_folder,'qc','another-worker')
        assert sync.lock_owned(step_folder,'qc','synthetic-step')
        sync.release(step_folder,'qc','synthetic-step')
        checks.append('native_step_lock_exclusion')
        crash_folder=temp/'crashed-step'
        code='import sys,time;sys.path.insert(0,'+repr(str(root/'tools'))+');from pathlib import Path;import sync;assert sync.acquire(Path('+repr(str(crash_folder))+'),"qc","synthetic-crash");print("READY",flush=True);time.sleep(60)'
        crash_worker=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE)
        try:assert crash_worker.stdout.readline().strip()==b'READY'
        finally:crash_worker.terminate();crash_worker.wait(timeout=5)
        assert sync.acquire(crash_folder,'qc','synthetic-recovery')
        sync.release(crash_folder,'qc','synthetic-recovery')
        assert not (crash_folder/'.sync_qc.lock').exists()
        checks.append('native_step_lock_crash_recovery')
        import library_sync as controller
        from library_resume import assert_workflow_available
        assert_workflow_available()  # Missing lock: preview must not create it.
        workflow=controller.RUNS/'workflow.lock'
        assert not workflow.exists()
        with platform.posix_lock(workflow):
            try:assert_workflow_available()
            except RuntimeError:pass
            else:raise AssertionError('Recovery preview ignored a live workflow lock')
        assert_workflow_available()
        checks.append('native_recovery_preview_workflow_lock')
        original=temp/'archive-source.md';original.write_bytes(b'verified synthetic source')
        archived=temp/'archive-target.md'
        with platform.posix_guard(original) as stream:
            platform.mac_move_locked(stream,original,archived)
            stream.seek(0);assert stream.read()==archived.read_bytes()
        assert not original.exists()
        original.write_bytes(b'another synthetic source')
        with platform.posix_guard(original) as stream:
            try:platform.mac_move_locked(stream,original,archived)
            except FileExistsError:pass
            else:raise AssertionError('Archive replaced an existing target')
            assert original.read_bytes()==b'another synthetic source'
            replacement=temp/'replacement.md';replacement.write_bytes(b'editor replacement')
            os.replace(replacement,original)
            try:platform.mac_move_locked(stream,original,temp/'unexpected.md')
            except RuntimeError:pass
            else:raise AssertionError('Archive moved a replaced source')
            assert original.read_bytes()==b'editor replacement'
        checks.append('native_source_archive_identity_and_no_overwrite')
        private=temp/'.secrets/plugin_bridge.json'
        write_private_manifest(private,{'token':'synthetic-test-only'})
        assert private.stat().st_mode & 0o777==0o600 and private.parent.stat().st_mode & 0o777==0o700
        checks.append('private_descriptor_permissions')
        # Dedicated test keychain; restore default and delete this one even on failure.
        old=command('/usr/bin/security','default-keychain','-d','user').strip('"')
        keychain=temp/'acceptance.keychain-db';password=uuid.uuid4().hex
        command('/usr/bin/security','create-keychain','-p',password,str(keychain))
        try:
            command('/usr/bin/security','unlock-keychain','-p',password,str(keychain))
            command('/usr/bin/security','default-keychain','-d','user','-s',str(keychain))
            native=platform.NativeKeychain()
            native.security.SecKeychainSetUserInteractionAllowed(False)
            first=_dpapi(b'synthetic payload one')
            store=temp/'.secrets/model_api.keychain';_atomic_bytes(store,first)
            assert b'synthetic payload' not in store.read_bytes()
            assert _dpapi(store.read_bytes(),decrypt=True)==b'synthetic payload one'
            second=_dpapi(b'synthetic payload two');_atomic_bytes(store,second)
            assert _dpapi(store.read_bytes(),decrypt=True)==b'synthetic payload two'
            try:_dpapi(first,decrypt=True)
            except RuntimeError:pass
            else:raise AssertionError('Old Keychain record was not cleaned up')
            checks.append('native_keychain_roundtrip_and_rotation')
            command('/usr/bin/security','lock-keychain',str(keychain))
            try:_dpapi(second,decrypt=True)
            except RuntimeError:pass
            else:raise AssertionError('Locked Keychain unexpectedly readable')
            command('/usr/bin/security','unlock-keychain','-p',password,str(keychain))
            assert _dpapi(second,decrypt=True)==b'synthetic payload two'
            checks.append('locked_keychain_fails_closed')
        finally:
            command('/usr/bin/security','default-keychain','-d','user','-s',old)
            command('/usr/bin/security','delete-keychain',str(keychain))
        source=temp/'synthetic.pdf';clean=temp/'upload.pdf'
        with fitz.open() as doc:
            page=doc.new_page();page.insert_text((72,72),'Synthetic scientific text')
            page.add_text_annot((100,100),'Synthetic user annotation')
            doc.save(source)
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        assert has_annotations(source)
        with fitz.open(source) as doc:strip_annotations(doc,clean)
        assert not has_annotations(clean) and hashlib.sha256(source.read_bytes()).hexdigest()==digest
        with fitz.open(clean) as doc:assert 'Synthetic scientific text' in doc[0].get_text()
        checks.append('native_pdf_upload_copy_preserves_source')
        app=installation/'app'
        bridge_env={**os.environ,'LIBRARY_SYNC_CONFIG':str(app/'library-sync.json')}
        probe=command(str(app/'runtime/bin/python3'),'-c',
                      'import sys,json;sys.path.insert(0,'+repr(str(app/'tools'))+');import library_paths as p,library_plugin_bridge as b;print(json.dumps({"data":str(p.DATA),"manifest":str(b.MANIFEST)}))',env=bridge_env)
        actual=json.loads(probe)
        assert Path(actual['data']).resolve()==(installation/'data').resolve(),actual
        checks.append('installed_runtime_configuration')
        bridge_error=temp/'bridge-stderr.log'
        bridge_stderr=cleanup.enter_context(bridge_error.open('wb'))
        bridge=subprocess.Popen([str(app/'runtime/bin/python3'),str(app/'tools/library_plugin_bridge.py'),'--serve','--apply'],
                                cwd=app,env=bridge_env,stdout=bridge_stderr,stderr=subprocess.STDOUT)
        def stop_bridge():
            if bridge.poll() is None:bridge.terminate()
            bridge.wait(timeout=5)
        cleanup.callback(stop_bridge)
        descriptor=installation/'data/.secrets/plugin_bridge.json'
        deadline=time.monotonic()+30
        while not descriptor.is_file() and bridge.poll() is None and time.monotonic()<deadline:time.sleep(0.1)
        assert descriptor.is_file(),'Native bridge did not publish descriptor (exit='+str(bridge.poll())+'): '+bridge_error.read_text(encoding='utf-8',errors='replace')[-2000:]
        endpoint=json.loads(descriptor.read_text())
        import requests
        session=requests.Session();session.trust_env=False
        response=session.get(f"http://127.0.0.1:{endpoint['port']}/v1/status",headers={'X-Library-Sync-Token':endpoint['token']},timeout=5)
        response.raise_for_status();checks.append('installed_native_bridge_status')
        command(str(app/'runtime/bin/python3'),str(app/'tools/library_plugin_bridge.py'),
                '--restart-idle',str(endpoint['pid']),'--apply',env=bridge_env)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            current=json.loads(descriptor.read_text())
            if current['pid']!=endpoint['pid']:break
            time.sleep(.1)
        else:raise RuntimeError('Replacement native bridge not confirmed')
        import psutil
        replacement=psutil.Process(current['pid'])
        cleanup.callback(lambda: replacement.terminate() if replacement.is_running() else None)
        assert str(app/'tools/library_plugin_bridge.py') in replacement.cmdline()
        replacement.terminate();replacement.wait(timeout=5)
        checks.append('native_idle_backend_replace')
        if args.zotero:
            stop_bridge()  # Gecko must now exercise its real nsIProcess launcher.
            def stop_gecko_bridge():
                import psutil
                current=json.loads(descriptor.read_text())
                try:
                    process=psutil.Process(current['pid'])
                    if str(app/'tools/library_plugin_bridge.py') in process.cmdline() and process.uids().real==os.getuid():
                        process.terminate();process.wait(timeout=5)
                except psutil.NoSuchProcess:pass
            cleanup.callback(stop_gecko_bridge)
            profile=temp/'zotero-profile';extensions=profile/'extensions';extensions.mkdir(parents=True)
            zotero_data=temp/'zotero-data';zotero_data.mkdir()
            plugin=root/'release/dist/library-sync-0.2.0-beta.16.xpi'
            assert plugin.is_file()
            (extensions/'library-sync@local.invalid.xpi').write_bytes(plugin.read_bytes())
            prefs={'extensions.zotero.dataDir':str(zotero_data),'extensions.zotero.useDataDir':True,
                   'extensions.zotero.firstRun':False,'extensions.zotero.sync.autoSync':False,
                   'extensions.autoDisableScopes':0,'extensions.enabledScopes':15,'extensions.update.enabled':False,
                   'app.update.enabled':False,'extensions.zotero.httpServer.enabled':False,
                   'extensions.library-sync.repo':str(app),'extensions.library-sync.pythonw':str(app/'runtime/bin/python3')}
            (profile/'user.js').write_text('\n'.join(f'user_pref({json.dumps(k)},{json.dumps(v)});' for k,v in prefs.items()))
            result=temp/'panel-result.json'
            import zipfile
            helper='''
function startup(){setTimeout(run,500);}
async function run(){let result={checks:[],failures:[],phase:'initialization'};
const save=()=>IOUtils.writeJSON(OUTPUT,result);
const phase=async value=>{result.phase=value;await save();};
const check=(k,v)=>(v?result.checks:result.failures).push(k);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const until=async fn=>{const end=Date.now()+15000;let value;while(!(value=fn())&&Date.now()<end)await sleep(100);return value;};
try{await save();await Zotero.initializationPromise;await phase('ui');await Zotero.uiReadyPromise;await new Promise(r=>setTimeout(r,1500));
let win=Zotero.getMainWindows()[0];let menu=[...win.document.querySelectorAll('menuitem')].find(e=>/Library Sync/.test(e.getAttribute('label')||''));
check('menu',!!menu);await phase('panel');if(menu){menu.dispatchEvent(new win.Event('command'));
let panel=await until(()=>Services.wm.getMostRecentWindow('library-sync:panel'));check('panel',!!panel);
await until(()=>panel?.librarySyncReady===true);check('ready',panel?.librarySyncReady===true);
check('version',panel?.LibrarySyncBridge?.VERSION==='0.2.0-beta.16');
await phase('bridge');const host=panel.librarySyncController.host;const client=host.createClient();const status=await client.connect(true);
check('bridge_launch_and_connection',!!status);
await phase('idle_upgrade');await until(()=>!panel.librarySyncController.busy && !panel.librarySyncController.pending && !Object.keys(panel.librarySyncController.catalogPending).length);
await host.restartBackend(status.backend.pid);client.close();
const replacement=host.createClient();const upgraded=await replacement.connect(true);
check('idle_backend_restart',upgraded.backend.pid!==status.backend.pid && upgraded.capabilities.includes('paper-recovery-v1'));
replacement.close();panel?.close();}}
catch(e){result.failures.push('exception');result.error=String(e.message).slice(0,400);}
finally{await phase('complete');Services.startup.quit(Ci.nsIAppStartup.eForceQuit);}}
function shutdown(){} function install(){} function uninstall(){}
'''
            manifest={'manifest_version':2,'name':'Isolated acceptance','version':'1.0','applications':{'zotero':{'id':'qa@local.invalid','update_url':'https://library-sync.invalid/updates.json','strict_min_version':'10.0','strict_max_version':'10.0.*'}}}
            with zipfile.ZipFile(extensions/'qa@local.invalid.xpi','w') as z:
                z.writestr('manifest.json',json.dumps(manifest));z.writestr('bootstrap.js','const OUTPUT='+json.dumps(str(result))+';\n'+helper)
            proc=subprocess.Popen([str(args.zotero),'-no-remote','-profile',str(profile),'-headless'],
                                  env={**bridge_env,'MOZ_NO_REMOTE':'1'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:proc.wait(timeout=150)
            except subprocess.TimeoutExpired:
                proc.terminate();proc.wait(timeout=10)
                state=json.loads(result.read_text()) if result.is_file() else {'phase':'before_helper_startup'}
                raise RuntimeError('Isolated Zotero startup timed out: '+json.dumps(state)) from None
            state=json.loads(result.read_text());assert not state['failures'],state
            checks.extend('zotero_'+name for name in state['checks'])
    args.output.write_text(json.dumps({'platform':sys.platform,'checks':checks,'model_requests':0,'user_profile_opened':False,'ok':True},indent=2))
    print('NATIVE_MAC_ACCEPTANCE_PASS',len(checks))


if __name__=='__main__':main()
