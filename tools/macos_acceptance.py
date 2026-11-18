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
    return subprocess.run(list(args), check=True, capture_output=True, **kwargs).stdout.decode().strip()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zotero',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if sys.platform!='darwin':raise RuntimeError('Native macOS required')
    root=Path(__file__).resolve().parents[1]
    checks=[]
    with tempfile.TemporaryDirectory(prefix='library-sync-acceptance-') as temporary, contextlib.ExitStack() as cleanup:
        temp=Path(temporary).resolve()
        config=temp/'config.json';library=temp/'Library';data=temp/'data'
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
        installation=temp/'installed'
        command(sys.executable,str(bundle/'app/release/install_macos.py'),'--source',str(bundle/'app'),
                '--install-dir',str(installation),'--library-dir',str(library),'--baseline-date','2026-01-01',
                '--git-user-name','Synthetic Acceptance','--git-user-email','fixture@example.invalid',
                '--init-library-git','--apply')
        receipt=json.loads((installation/'INSTALL_RECEIPT.json').read_text())
        assert receipt['runtime_ready'] is True
        checks.append('offline_installer_runtime')

        identity=progress.process_identity(os.getpid())
        assert progress.owner_matches(identity);checks.append('native_process_identity')
        worker=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
        try:
            child=progress.process_identity(worker.pid);assert progress.owner_matches(child)
        finally:
            worker.terminate();worker.wait(timeout=5)
        assert not progress.owner_matches(child);checks.append('exited_process_rejected')
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
        bridge=subprocess.Popen([str(app/'runtime/bin/python3'),str(app/'tools/library_plugin_bridge.py'),'--serve','--apply'],
                                cwd=app,env={**os.environ,'LIBRARY_SYNC_CONFIG':str(app/'library-sync.json')},
                                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def stop_bridge():
            if bridge.poll() is None:bridge.terminate()
            bridge.wait(timeout=5)
        cleanup.callback(stop_bridge)
        descriptor=installation/'data/.secrets/plugin_bridge.json'
        deadline=time.monotonic()+12
        while not descriptor.is_file() and bridge.poll() is None and time.monotonic()<deadline:time.sleep(0.1)
        assert descriptor.is_file(),'Native bridge did not publish descriptor'
        endpoint=json.loads(descriptor.read_text())
        import requests
        session=requests.Session();session.trust_env=False
        response=session.get(f"http://127.0.0.1:{endpoint['port']}/v1/status",headers={'X-Library-Sync-Token':endpoint['token']},timeout=5)
        response.raise_for_status();checks.append('installed_native_bridge_status')
        if args.zotero:
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
async function run(){let result={checks:[],failures:[]};const check=(k,v)=>(v?result.checks:result.failures).push(k);
try{await Zotero.initializationPromise;await Zotero.uiReadyPromise;await new Promise(r=>setTimeout(r,1500));
let win=Zotero.getMainWindows()[0];let menu=[...win.document.querySelectorAll('menuitem')].find(e=>/Library Sync/.test(e.getAttribute('label')||''));
check('menu',!!menu);if(menu){menu.dispatchEvent(new win.Event('command'));await new Promise(r=>setTimeout(r,1000));
let panel=Services.wm.getMostRecentWindow('library-sync:panel');check('panel',!!panel);check('ready',panel?.librarySyncReady===true);
check('version',panel?.LibrarySyncBridge?.VERSION==='0.2.0-beta.16');
const host=panel.librarySyncController.host;const client=host.createClient();const status=await client.connect(false);
check('bridge_connection',!!status);client.close();panel?.close();}}
catch(e){result.failures.push('exception');result.error=String(e.message).slice(0,400);}
finally{await IOUtils.writeJSON(OUTPUT,result);Services.startup.quit(Ci.nsIAppStartup.eForceQuit);}}
function shutdown(){} function install(){} function uninstall(){}
'''
            manifest={'manifest_version':2,'name':'Isolated acceptance','version':'1.0','applications':{'zotero':{'id':'qa@local.invalid','update_url':'https://library-sync.invalid/updates.json','strict_min_version':'10.0','strict_max_version':'10.0.*'}}}
            with zipfile.ZipFile(extensions/'qa@local.invalid.xpi','w') as z:
                z.writestr('manifest.json',json.dumps(manifest));z.writestr('bootstrap.js','const OUTPUT='+json.dumps(str(result))+';\n'+helper)
            proc=subprocess.Popen([str(args.zotero),'-no-remote','-profile',str(profile),'-headless'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:proc.wait(timeout=60)
            except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=10);raise
            state=json.loads(result.read_text());assert not state['failures'],state
            checks.extend('zotero_'+name for name in state['checks'])
    args.output.write_text(json.dumps({'platform':sys.platform,'checks':checks,'model_requests':0,'user_profile_opened':False,'ok':True},indent=2))
    print('NATIVE_MAC_ACCEPTANCE_PASS',len(checks))


if __name__=='__main__':main()
