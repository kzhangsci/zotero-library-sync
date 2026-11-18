"""Archive exact upstream sources; bootstrap mode records the first official MuPDF hash."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bootstrap-mupdf',action='store_true')
    args=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    lock=json.loads((root/'release/third-party-sources.lock.json').read_text(encoding='utf-8'))
    target=root/'release/vendor/sources';target.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for row in lock['sources']:
        file=target/row['filename']
        if not file.is_file():
            subprocess.run(['curl','--fail','--location','--retry','2','--connect-timeout','10','--max-time','180',
                            row['url'],'--output',str(file)],check=True)
        digest=hashlib.sha256(file.read_bytes()).hexdigest()
        if row['sha256']:
            if digest!=row['sha256']:raise ValueError('Source checksum failed: '+row['name'])
        elif not(args.bootstrap_mupdf and row['name']=='MuPDF'):
            raise ValueError('Source hash is not pinned')
        if row['name']=='MuPDF':
            with tarfile.open(file,'r:gz') as archive:
                members=archive.getmembers()
                version=next(m for m in members if m.name.endswith('/include/mupdf/fitz/version.h'))
                if b'1.26.12' not in archive.extractfile(version).read():raise ValueError('MuPDF source version mismatch')
                if not any('/thirdparty/' in m.name for m in members):raise ValueError('MuPDF third-party tree missing')
        manifest.append({**row,'sha256':digest,'bytes':file.stat().st_size})
        print('VERIFIED_SOURCE',row['name'],row['version'],flush=True)
    out=root/'release/dist';out.mkdir(parents=True,exist_ok=True)
    version=json.loads((root/'tools/zotero_library_sync/manifest.json').read_text(encoding='utf-8'))['version']
    record={'schema':1,'version':version,'bootstrap':args.bootstrap_mupdf,'sources':manifest}
    (out/'third-party-sources-manifest.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    with zipfile.ZipFile(out/f'library-sync-{version}-third-party-sources.zip','w',compression=zipfile.ZIP_STORED) as z:
        for row in manifest:z.write(target/row['filename'],'sources/'+row['filename'])
        z.writestr('SOURCES.json',json.dumps(record,indent=2))
        for name in ('THIRD_PARTY_SOURCE_GUIDE.md','DISTRIBUTION_LICENSE.md','AGPL-3.0.txt'):
            z.write(root/'release'/name,name)
    print('SOURCE_ARCHIVE_READY')


if __name__=='__main__':main()
