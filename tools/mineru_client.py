# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import argparse
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
API = 'https://mineru.net/api/v4'
UA = 'lit_migration-sync/0.1'

def token() -> str:
    if __package__:
        from .mineru_credentials import CredentialError, load_token
    else:
        from mineru_credentials import CredentialError, load_token
    try:
        value = load_token()
    except CredentialError as error:
        raise SystemExit(str(error)) from None
    if value is None:
        raise SystemExit('MinerU token missing: set MINERU_API_TOKEN or save a key through the Zotero plugin')
    return value

def _req(method: str, url: str, data: bytes | None=None, headers: dict | None=None, timeout: int=120, retries: int=5):
    h = {'User-Agent': UA}
    h.update(headers or {})
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return (r.status, r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(30 * (attempt + 1))
                continue
            raise

def api_json(method: str, path: str, payload: dict | None=None) -> dict:
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    status, body = _req(method, API + path, data, {'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json', 'Accept': '*/*'})
    d = json.loads(body.decode('utf-8'))
    if d.get('code') not in (0, '0', None) or 'data' not in d:
        raise RuntimeError(f"MinerU API error {path}: code={d.get('code')} msg={d.get('msg')}")
    return d['data']

def strip_annotations(src, dst: Path, from_page: int=0, to_page: int=-1) -> Path:
    import fitz
    dst.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as out:
        out.insert_pdf(src, from_page=from_page, to_page=to_page, annots=False)
        out.save(str(dst), garbage=3, deflate=True)
    return dst

def has_annotations(pdf: Path) -> bool:
    try:
        import fitz
        with fitz.open(str(pdf)) as doc:
            return any((page.first_annot for page in doc))
    except Exception:
        return False

def upload_pdf(url: str, pdf: Path, attempts: int=3) -> None:
    import requests
    for attempt in range(attempts):
        try:
            with open(pdf, 'rb') as fh:
                resp = requests.put(url, data=fh, timeout=600)
        except requests.exceptions.SSLError:
            raise RuntimeError('MinerU upload TLS verification failed') from None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt + 1 == attempts:
                raise RuntimeError(f'MinerU upload connection failed after {attempts} attempts') from None
            print(f'MinerU upload interrupted; retry {attempt + 2}/{attempts}', flush=True)
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f'MinerU upload failed: HTTP {resp.status_code}')
        return

def parse_pdf(pdf: Path, out_dir: Path, lang: str='en', pages: str | None=None, model_version: str='vlm', poll: int=10, max_wait: int=1800, ocr: bool=False) -> Path:
    pdf = Path(pdf)
    out = Path(out_dir) / pdf.stem
    out.mkdir(parents=True, exist_ok=True)
    upload = pdf
    if has_annotations(pdf):
        import fitz
        with fitz.open(str(pdf)) as src:
            upload = strip_annotations(src, out / 'upload' / pdf.name)
    file_spec = {'name': pdf.name, 'is_ocr': bool(ocr), 'data_id': pdf.stem[:64]}
    if pages:
        file_spec['page_ranges'] = pages
    d = api_json('POST', '/file-urls/batch', {'enable_formula': True, 'enable_table': True, 'language': lang, 'model_version': model_version, 'files': [file_spec]})
    batch_id, urls = (d['batch_id'], d['file_urls'])
    job = {'batch_id': batch_id, 'pdf': str(pdf), 'annotations_stripped': upload != pdf, 'pages': pages, 'ocr': bool(ocr), 'submitted': time.strftime('%Y-%m-%d %H:%M:%S'), 'max_wait': max_wait}
    (out / '_job.json').write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding='utf-8')
    upload_pdf(urls[0], upload)
    t0 = time.time()
    while True:
        res = api_json('GET', f'/extract-results/batch/{batch_id}')
        items = res.get('extract_result', [])
        st = items[0] if items else {}
        state = st.get('state')
        if state == 'done':
            zurl = st['full_zip_url']
            break
        if state == 'failed':
            raise RuntimeError(f"MinerU failed: {st.get('err_msg')}")
        if time.time() - t0 > max_wait:
            raise RuntimeError(f'MinerU timeout after {int(time.time() - t0)}s (batch {batch_id}, state {state})')
        time.sleep(poll)
    _, blob = _req('GET', zurl, timeout=600)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(out)
    md = next(out.rglob('full.md'), None) or next(out.rglob('*.md'), None)
    if not md:
        raise RuntimeError('no markdown in MinerU zip')
    return md
DUP_SIGNATURE = (10, 3)

def has_dup_artifact(md_text: str) -> int:
    import re
    n = 0
    for p in re.split('\\n\\s*\\n', md_text):
        if '<sub>' in p and len(re.findall('<sup>', p)) >= DUP_SIGNATURE[0] and (len(re.findall('</sup>\\s*<sup>', p)) >= 5):
            n += 1
    return n

def page_count(pdf: Path) -> int:
    try:
        import fitz
        with fitz.open(str(pdf)) as doc:
            return int(doc.page_count)
    except Exception:
        return 0
MAX_PAGES_PER_FILE = 200
PARTS_PER_BATCH = 10
FAST_PAGES = 10

def _download_zip(zurl: str, out: Path) -> Path:
    _, blob = _req('GET', zurl, timeout=600)
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(out)
    md = next(out.rglob('full.md'), None) or next(out.rglob('*.md'), None)
    if not md:
        raise RuntimeError('no markdown in MinerU zip')
    return md

def parse_pdf_parts(pdf: Path, out_dir: Path, lang: str='en', part_pages: int=FAST_PAGES, model_version: str='vlm', poll: int=10, max_wait: int | None=None, ocr: bool=False) -> Path:
    pdf = Path(pdf)
    n = page_count(pdf)
    out = Path(out_dir) / pdf.stem
    out.mkdir(parents=True, exist_ok=True)
    ranges = [f'{a}-{min(a + part_pages - 1, n)}' for a in range(1, n + 1, part_pages)]
    import fitz
    parts_dir = out / 'parts'
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_files = []
    with fitz.open(str(pdf)) as src:
        for i, r in enumerate(ranges):
            a, b = (int(x) for x in r.split('-'))
            pf = parts_dir / f'{pdf.stem[:50]}_p{i:03d}.pdf'
            if not pf.is_file() or has_annotations(pf):
                strip_annotations(src, pf, a - 1, b - 1)
            part_files.append(pf)
    files = [{'name': pf.name, 'is_ocr': bool(ocr), 'data_id': pf.stem} for pf in part_files]
    max_wait = max_wait or int(min(5400, 600 + 90 * min(len(ranges), PARTS_PER_BATCH)))
    import requests
    by_id: dict = {}
    batch_ids = []
    for g in range(0, len(files), PARTS_PER_BATCH):
        group, group_files = (files[g:g + PARTS_PER_BATCH], part_files[g:g + PARTS_PER_BATCH])
        d = api_json('POST', '/file-urls/batch', {'enable_formula': True, 'enable_table': True, 'language': lang, 'model_version': model_version, 'files': group})
        batch_id, urls = (d['batch_id'], d['file_urls'])
        batch_ids.append(batch_id)
        (out / '_job.json').write_text(json.dumps({'batch_ids': batch_ids, 'pdf': str(pdf), 'pages': n, 'parts': ranges, 'ocr': bool(ocr), 'submitted': time.strftime('%Y-%m-%d %H:%M:%S'), 'max_wait': max_wait}, ensure_ascii=False, indent=1), encoding='utf-8')
        for u, pf in zip(urls, group_files):
            upload_pdf(u, pf)
        t0 = time.time()
        while True:
            res = api_json('GET', f'/extract-results/batch/{batch_id}')
            items = res.get('extract_result', [])
            states = [x.get('state') for x in items]
            if items and all((st == 'done' for st in states)):
                break
            failed = [x for x in items if x.get('state') == 'failed']
            if failed:
                raise RuntimeError(f"MinerU failed: {failed[0].get('err_msg')} (part {failed[0].get('data_id')})")
            if time.time() - t0 > max_wait:
                raise RuntimeError(f'MinerU timeout after {int(time.time() - t0)}s (batch {batch_id}, states {states})')
            time.sleep(poll)
        by_id.update({x.get('data_id'): x for x in items})
    texts = []
    (out / 'images').mkdir(parents=True, exist_ok=True)
    for f in files:
        md = _download_zip(by_id[f['data_id']]['full_zip_url'], out / ('part_' + f['data_id'][-4:]))
        t = md.read_text(encoding='utf-8', errors='ignore').strip()
        if texts and t and (not texts[-1].rstrip().endswith(('.', '!', '?', ':', ';', '”', ')', ']', '|', '$'))) and t[0].islower():
            texts[-1] = texts[-1].rstrip() + ' ' + t
        else:
            texts.append(t)
        for img in (md.parent / 'images').glob('*'):
            shutil.copy2(img, out / 'images' / img.name)
    full = out / 'full.md'
    full.write_text('\n\n'.join(texts) + '\n', encoding='utf-8')
    return full

def parse_pdf_any(pdf: Path, out_dir: Path, lang: str='en', max_pages: int=FAST_PAGES, **kw) -> Path:
    pdf = Path(pdf)
    n = page_count(pdf)
    if n <= max_pages:
        kw.setdefault('max_wait', 1800)
        return parse_pdf(pdf, out_dir, lang, **kw)
    return parse_pdf_parts(pdf, out_dir, lang, part_pages=max_pages, **kw)

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('parse')
    p.add_argument('pdf')
    p.add_argument('--out', required=True)
    p.add_argument('--lang', default='en')
    p.add_argument('--pages', default=None)
    p.add_argument('--model-version', default='vlm')
    a = ap.parse_args()
    md = parse_pdf(Path(a.pdf), Path(a.out), a.lang, a.pages, a.model_version) if a.pages else parse_pdf_any(Path(a.pdf), Path(a.out), a.lang, model_version=a.model_version)
    imgs = len(list(md.parent.glob('images/*')))
    print(f'{md}  (images: {imgs}, chars: {md.stat().st_size})')
    return 0
if __name__ == '__main__':
    sys.exit(main())
