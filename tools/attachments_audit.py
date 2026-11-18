# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPORTS, add_common_args, year_of, zotero_data_dir
try:
    import fitz
except Exception:
    fitz = None
API = 'http://127.0.0.1:23119/api/users/0'
STORED = ('imported_file', 'imported_url')
MARKUP_TYPES = {'Highlight', 'Underline', 'StrikeOut', 'Squiggly', 'Text', 'FreeText', 'Ink', 'Square', 'Circle', 'Line', 'Polygon', 'PolyLine', 'Stamp', 'Caret', 'FileAttachment', 'Redact', 'Sound', 'Movie'}
IGNORE_TYPES = {'Link', 'Widget', 'Popup'}
HIGHLIGHT_HUES = {'yellow': (40, 75), 'green': (75, 170), 'cyan': (170, 200), 'blue': (200, 250), 'pink': (280, 350), 'red': (350, 360), 'red2': (0, 15), 'orange': (15, 40)}
MIN_SAT = 0.25
MIN_VAL = 0.5
MIN_W = 14.0
MIN_H, MAX_H = (3.0, 34.0)
MIN_ASPECT = 2.5
MAX_CAND_PER_PAGE = 60
FIGURE_NOTE = 'figure-heavy page ignored'
LINE_MIN_WIDTH_FRAC = 0.25
LINE_MIN_WORDS = 4
LINE_MIN_CHARS = 12
COVER_MIN_WORDS = 2
COVER_MIN_FRAC = 0.5
READER_RX = re.compile('acrobat|foxit|xodo|pdf-?xchange|pdfxc|quartz|preview|skim|goodnotes|notability|drawboard|pdfelement|nitro|kami|liquidtext|marginnote|zotero|mendeley|iannotate|flexcil|wps', re.I)
RX_SUPP = re.compile('(?:^|[-_ ])(?:supp(?:l(?:ement(?:ary|al)?)?)?|(?:supplement(?:ary|al)?|supporting)[-_ ]*(?:information|materials?|data|files?)|esm|si|sm|moesm|appendix|附录|补充)(?:[-_ .0-9]|$)', re.I)
RX_PRE = re.compile('preprint|arxiv|biorxiv|essoar|earthar[xX]iv|submitted|accepted[-_ ]?manuscript', re.I)
RX_MANU = re.compile('manuscript|draft|原稿|手稿', re.I)
RX_TRANS = re.compile('translation|translated|译文|中译|英译', re.I)
FULLTEXT_EXT = {'.pdf', '.caj'}
AUX_ROLE = {'.html': 'snapshot', '.htm': 'snapshot', '.mht': 'snapshot', '.m': 'code', '.py': 'code', '.r': 'code', '.ipynb': 'code', '.sh': 'code', '.txt': 'readme', '.md': 'readme', '.csv': 'data', '.nc': 'data', '.mat': 'data', '.zip': 'data', '.xlsx': 'data', '.xls': 'data', '.docx': 'doc', '.doc': 'doc', '.pptx': 'slides', '.ppt': 'slides'}
ROLE_SUFFIXES = [('annotated', re.compile('^_annotated\\d*$', re.I)), ('supplement', re.compile('^_supplement\\d*$', re.I)), ('preprint', re.compile('^_preprint\\d*$', re.I)), ('manuscript', re.compile('^_manuscript\\d*$', re.I)), ('translation', re.compile('^_translation\\d*$', re.I)), ('lowres', re.compile('^_lowres\\d*$', re.I))]

def rgb_bucket(c) -> str:
    if not c or len(c) < 3:
        return ''
    r, g, b = (float(c[0]), float(c[1]), float(c[2]))
    mx, mn = (max(r, g, b), min(r, g, b))
    if mx - mn < MIN_SAT or mx < MIN_VAL:
        return ''
    d = mx - mn
    if mx == r:
        h = 60 * ((g - b) / d) % 360
    elif mx == g:
        h = 60 * ((b - r) / d) + 120
    else:
        h = 60 * ((r - g) / d) + 240
    for name, (lo, hi) in HIGHLIGHT_HUES.items():
        if lo <= h < hi:
            return name.rstrip('2')
    return ''

def scan_pdf(path: str, max_draw_pages: int=400) -> dict:
    out = {'pages': '', 'encrypted': '', 'err': '', 'producer': '', 'creator': '', 'embed_markup': 0, 'embed_types': '', 'embed_all_types': '', 'embed_pages': 0, 'embed_pages_list': '', 'embed_authors': '', 'embed_sample': '', 'flat_pages': 0, 'flat_marks': 0, 'flat_colors': '', 'flat_examples': '', 'flat_note': '', 'flat_raw': 0}
    if fitz is None:
        out['err'] = 'PyMuPDF missing'
        return out
    try:
        doc = fitz.open(path)
    except Exception as e:
        out['err'] = f'open: {str(e)[:60]}'
        return out
    try:
        out['pages'] = doc.page_count
        out['encrypted'] = bool(doc.is_encrypted)
        md = doc.metadata or {}
        out['producer'] = (md.get('producer') or '')[:60]
        out['creator'] = (md.get('creator') or '')[:60]
        sub: Counter = Counter()
        authors: Counter = Counter()
        samples: list[str] = []
        mark_pages: list[int] = []
        flat_pages, flat_marks, flat_raw, colors, examples = (0, 0, 0, Counter(), [])
        notes = set()
        for i, page in enumerate(doc):
            n_mark_here = 0
            try:
                for a in page.annots() or []:
                    try:
                        name = a.type[1]
                    except Exception:
                        name = '?'
                    sub[name] += 1
                    if name in MARKUP_TYPES:
                        n_mark_here += 1
                        try:
                            info = a.info or {}
                            authors[(info.get('title') or '(无署名)')[:24]] += 1
                            c = (info.get('content') or '').strip().replace('\n', ' ')
                            if c and len(samples) < 3:
                                samples.append(f'{name}:{c[:40]}')
                        except Exception:
                            pass
            except Exception:
                sub['?err'] += 1
            if n_mark_here:
                mark_pages.append(i + 1)
            if max_draw_pages and i >= max_draw_pages:
                notes.add(f'drawings scanned for the first {max_draw_pages} pages only')
                continue
            try:
                drawings = page.get_drawings(extended=True)
            except Exception:
                try:
                    drawings = page.get_drawings()
                except Exception:
                    continue
            pw, ph = (page.rect.width, page.rect.height)
            cands = []
            for d in drawings:
                if d.get('type') not in ('f', 'fs'):
                    continue
                fill = d.get('fill')
                if not fill:
                    continue
                r = d.get('rect')
                if r is None:
                    continue
                w, h = (r.width, r.height)
                if w < MIN_W or h < MIN_H or h > MAX_H or (w / max(h, 0.01) < MIN_ASPECT):
                    continue
                if w > 0.92 * pw or r.y0 < 0.04 * ph or r.y1 > 0.97 * ph:
                    continue
                bucket = rgb_bucket(fill)
                multiply = str(d.get('blendmode') or '') == 'Multiply'
                if not bucket and (not multiply):
                    continue
                cands.append((r, bucket or 'multiply', multiply))
            if not cands:
                continue
            if len(cands) > MAX_CAND_PER_PAGE:
                notes.add(FIGURE_NOTE)
                continue
            words = page.get_text('words') or []
            if not words:
                continue
            lines: dict[tuple, list] = defaultdict(list)
            for w in words:
                lines[w[5], w[6]].append(w)
            line_box = {k: (min((w[0] for w in ws)), min((w[1] for w in ws)), max((w[2] for w in ws)), max((w[3] for w in ws))) for k, ws in lines.items()}
            body = {k for k, ws in lines.items() if line_box[k][2] - line_box[k][0] >= LINE_MIN_WIDTH_FRAC * pw and (len(ws) >= LINE_MIN_WORDS or sum((len(w[4]) for w in ws)) >= LINE_MIN_CHARS)}
            hits_here = 0
            flat_raw += len(cands)
            for r, bucket, _mult in cands:
                per_line: dict[tuple, list] = defaultdict(list)
                for w in words:
                    wx0, wy0, wx1, wy1, txt = (w[0], w[1], w[2], w[3], w[4])
                    if len(txt) < 1:
                        continue
                    ix0, iy0 = (max(r.x0, wx0), max(r.y0, wy0))
                    ix1, iy1 = (min(r.x1, wx1), min(r.y1, wy1))
                    if ix1 <= ix0 or iy1 <= iy0:
                        continue
                    warea = max((wx1 - wx0) * (wy1 - wy0), 0.01)
                    if (ix1 - ix0) * (iy1 - iy0) / warea >= 0.3:
                        per_line[w[5], w[6]].append(w)
                hit = False
                sample = ''
                for k, ws in per_line.items():
                    if k not in body:
                        continue
                    lw = line_box[k][2] - line_box[k][0]
                    covered_w = sum((min(r.x1, w[2]) - max(r.x0, w[0]) for w in ws))
                    if len(ws) >= COVER_MIN_WORDS or (lw > 0 and covered_w / lw >= COVER_MIN_FRAC):
                        hit = True
                        sample = ' '.join((w[4] for w in ws))[:28]
                        break
                if hit:
                    hits_here += 1
                    colors[bucket] += 1
                    if len(examples) < 6:
                        examples.append(f'p{i + 1}:{bucket}:{sample}')
            if hits_here:
                flat_pages += 1
                flat_marks += hits_here
        out['embed_markup'] = sum((v for k, v in sub.items() if k in MARKUP_TYPES))
        out['embed_types'] = ','.join((f'{k}:{v}' for k, v in sub.most_common() if k in MARKUP_TYPES))
        out['embed_all_types'] = ','.join((f'{k}:{v}' for k, v in sub.most_common() if k not in IGNORE_TYPES))
        out['embed_pages'] = len(mark_pages)
        out['embed_pages_list'] = ','.join((str(p) for p in mark_pages[:25]))
        out['embed_authors'] = ','.join((f'{k}:{v}' for k, v in authors.most_common(4)))
        out['embed_sample'] = ' | '.join(samples)
        out['flat_pages'], out['flat_marks'], out['flat_raw'] = (flat_pages, flat_marks, flat_raw)
        out['flat_colors'] = ','.join((f'{k}:{v}' for k, v in colors.most_common()))
        out['flat_examples'] = ' | '.join(examples)
        out['flat_note'] = '; '.join(sorted(notes))
    except Exception as e:
        out['err'] = f'scan: {str(e)[:60]}'
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out

def role_of(filename: str, ck: str, n_pdfs: int) -> tuple[str, str, str, str]:
    ext = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    stem = filename[:len(filename) - len(ext)] if ext else filename
    if ck and stem == ck:
        return ('primary', 'stem == citekey', 'primary', '')
    if ck and stem.startswith(ck):
        suffix = stem[len(ck):]
        for role, rx in ROLE_SUFFIXES:
            if rx.match(suffix):
                return (role, f'stem == citekey + {suffix}', role, '')
        return ('other', f'citekey + unknown suffix {suffix!r}', '待用户定', '')
    if ext not in FULLTEXT_EXT:
        return (AUX_ROLE.get(ext, 'aux'), f"辅助文件（{ext or '无扩展名'}）", AUX_ROLE.get(ext, 'aux'), '')
    if ext == '.caj':
        return ('caj', 'CAJ 格式（MinerU 无法解析）', 'caj', f'{ck}{ext}' if ck else '')
    if ext == '.pdf' and n_pdfs == 1:
        return ('other', 'not named after the key', 'primary', f'{ck}{ext}')
    if RX_SUPP.search(stem):
        return ('other', 'not named after the key', 'supplement', f'{ck}_supplement{ext}')
    if RX_PRE.search(stem):
        return ('other', 'not named after the key', 'preprint', f'{ck}_preprint{ext}')
    if RX_MANU.search(stem):
        return ('other', 'not named after the key', 'manuscript', f'{ck}_manuscript{ext}')
    if RX_TRANS.search(stem):
        return ('other', 'not named after the key', 'translation', f'{ck}_translation{ext}')
    return ('other', 'not named after the key', '待用户定', '')

def get_all(path: str) -> list:
    import urllib.parse
    import urllib.request
    out, start = ([], 0)
    while True:
        url = API + path + '?' + urllib.parse.urlencode({'limit': 100, 'start': start})
        with urllib.request.urlopen(url, timeout=120) as r:
            batch, total = (json.load(r), int(r.headers.get('Total-Results', '0') or 0))
        out.extend(batch)
        start += len(batch)
        if not batch or start >= total:
            break
    return out
CSV_COLS = ['item_key', 'citekey', 'item_type', 'title', 'year', 'item_tags', 'n_atts', 'n_pdfs', 'att_key', 'att_title', 'filename', 'ext', 'content_type', 'link_mode', 'att_tags', 'size_bytes', 'exists', 'role', 'role_basis', 'suggested_role', 'suggested_name', 'pages', 'encrypted', 'embed_markup', 'embed_types', 'embed_pages', 'embed_pages_list', 'embed_all_types', 'embed_authors', 'embed_sample', 'flat_pages', 'flat_marks', 'flat_raw', 'flat_colors', 'flat_examples', 'flat_note', 'zot_annots', 'zot_annot_types', 'producer', 'creator', 'reader_hint', 'verdict', 'err']

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dump', default='', help='API items JSON; empty = read the local API now (read-only)')
    ap.add_argument('--jobs', type=int, default=4, help='parallel PDF scanners (default 4; sync workers are running)')
    ap.add_argument('--max-draw-pages', type=int, default=400, help='cap for the flattened-mark scan on very long PDFs')
    ap.add_argument('--no-cache', action='store_true', help='ignore reports/_cache/attachments_audit_pdfcache.json')
    ap.add_argument('--date', default=dt.date.today().strftime('%Y%m%d'), help='suffix of the report file names')
    ap.add_argument('--compare', default=str(REPORTS / 'phase2_attachments_review.csv'), help="phase-2 attachment table to compare the annotation detection against ('' = skip)")
    add_common_args(ap)
    a = ap.parse_args()
    items = json.loads(Path(a.dump).read_text(encoding='utf-8')) if a.dump else get_all('/items')
    reg = {x['key']: x['data'] | {'_parsedDate': (x.get('meta') or {}).get('parsedDate') or ''} for x in items if x['data']['itemType'] not in ('attachment', 'note', 'annotation')}
    atts_by_parent: dict[str, list[dict]] = defaultdict(list)
    for x in items:
        d = x['data']
        if d['itemType'] == 'attachment' and d.get('parentItem'):
            atts_by_parent[d['parentItem']].append(d | {'key': x['key']})
    zot_ann: dict[str, Counter] = defaultdict(Counter)
    for x in items:
        d = x['data']
        if d['itemType'] == 'annotation' and d.get('parentItem'):
            zot_ann[d['parentItem']][d.get('annotationType') or '?'] += 1
    storage = zotero_data_dir() / 'storage'
    cache_path = REPORTS / '_cache' / 'attachments_audit_pdfcache.json'
    cache: dict = {}
    if cache_path.is_file() and (not a.no_cache):
        try:
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
        except Exception:
            cache = {}
    rows: list[dict] = []
    for ikey, d in reg.items():
        ck = d.get('citationKey') or ''
        stored = [x for x in atts_by_parent.get(ikey, []) if x.get('linkMode') in STORED and x.get('filename')]
        n_pdfs = sum((1 for x in stored if x.get('contentType') == 'application/pdf' or x['filename'].lower().endswith('.pdf')))
        for at in stored:
            fn = at['filename']
            ext = '.' + fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
            role, basis, sug_role, sug_name = role_of(fn, ck, n_pdfs)
            p = storage / at['key'] / fn
            try:
                st = p.stat()
                exists, size, mtime = (True, st.st_size, int(st.st_mtime))
            except OSError:
                exists, size, mtime = (False, 0, 0)
            za = zot_ann.get(at['key'], Counter())
            rows.append({'item_key': ikey, 'citekey': ck, 'item_type': d['itemType'], 'title': (d.get('title') or '')[:90], 'year': year_of(d), 'item_tags': ','.join((t['tag'] for t in d.get('tags') or [])), 'n_atts': len(stored), 'n_pdfs': n_pdfs, 'att_key': at['key'], 'att_title': (at.get('title') or '')[:60], 'filename': fn, 'ext': ext, 'content_type': at.get('contentType') or '', 'link_mode': at.get('linkMode') or '', 'att_tags': ','.join((t['tag'] for t in at.get('tags') or [])), 'size_bytes': size, 'exists': exists, 'role': role, 'role_basis': basis, 'suggested_role': sug_role, 'suggested_name': sug_name, 'zot_annots': sum(za.values()), 'zot_annot_types': ','.join((f'{k}:{v}' for k, v in za.most_common())), '_path': str(p), '_mtime': mtime})
    if a.limit:
        rows = rows[:a.limit]
    todo = [r for r in rows if r['exists'] and r['ext'] == '.pdf']
    scan_keys = {id(r): f"{r['att_key']}|{r['filename']}|{r['size_bytes']}|{r['_mtime']}|v2" for r in todo}
    need = [r for r in todo if scan_keys[id(r)] not in cache]
    print(f'stored attachments: {len(rows)}  PDFs on disk: {len(todo)}  to scan: {len(need)} (cached {len(todo) - len(need)})')
    if need:
        with cf.ProcessPoolExecutor(max_workers=max(1, a.jobs)) as ex:
            futs = {ex.submit(scan_pdf, r['_path'], a.max_draw_pages): r for r in need}
            for n, fut in enumerate(cf.as_completed(futs), 1):
                r = futs[fut]
                try:
                    cache[scan_keys[id(r)]] = fut.result()
                except Exception as e:
                    cache[scan_keys[id(r)]] = {'err': f'worker: {str(e)[:60]}'}
                if n % 200 == 0 or n == len(need):
                    print(f'  scanned {n}/{len(need)}', flush=True)
    if a.apply and (not a.no_cache):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
    blank = {k: '' for k in ('pages', 'encrypted', 'err', 'producer', 'creator', 'embed_types', 'embed_pages_list', 'embed_all_types', 'embed_authors', 'embed_sample', 'flat_colors', 'flat_examples', 'flat_note')}
    blank |= {k: 0 for k in ('embed_markup', 'embed_pages', 'flat_pages', 'flat_marks', 'flat_raw')}
    for r in rows:
        s = cache.get(scan_keys.get(id(r), ''), blank)
        r.update({k: s.get(k, blank.get(k, '')) for k in blank})
        hint = []
        for f in (r.get('producer') or '', r.get('creator') or ''):
            m = READER_RX.search(f)
            if m:
                hint.append(m.group(0))
        r['reader_hint'] = ','.join(sorted(set((h.lower() for h in hint))))
        if r['embed_markup']:
            r['verdict'] = 'embedded'
        elif r['zot_annots']:
            r['verdict'] = 'zotero-reader'
        elif r['flat_marks']:
            r['verdict'] = 'suspect-flattened'
        elif not r['exists']:
            r['verdict'] = 'missing-file'
        elif r['ext'] != '.pdf':
            r['verdict'] = 'not-pdf'
        else:
            r['verdict'] = 'clean'
    md = summarize(rows, reg, a.date) + compare_phase2(rows, a.compare)
    if a.apply:
        csv_path = REPORTS / f'attachments_audit_{a.date}.csv'
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction='ignore')
            w.writeheader()
            w.writerows(rows)
        (REPORTS / f'attachments_audit_{a.date}.md').write_text(md, encoding='utf-8')
        print(f'wrote {csv_path} and attachments_audit_{a.date}.md')
    else:
        print('(dry run: nothing written; add --apply to write reports/)')
    print('\n'.join(md.splitlines()[:40]))
    return 0

def compare_phase2(rows: list[dict], path: str) -> str:
    if not path or not Path(path).is_file():
        return ''
    with open(path, encoding='utf-8-sig') as f:
        old = list(csv.DictReader(f))
    old_ann = {r['att_key'] for r in old if (r.get('annots') or '0') not in ('', '0')}
    old_all = {r['att_key'] for r in old}
    pdfs = [r for r in rows if r['ext'] == '.pdf']
    now = {r['att_key']: r for r in pdfs}
    new_ann = {k for k, r in now.items() if r['embed_markup'] or r['zot_annots'] or r['flat_marks']}
    tagged = {r['att_key'] for r in rows if 'annotated' in (r['att_tags'] or '')}
    added = new_ann - old_ann
    layer = Counter()
    for k in added:
        r = now[k]
        layer['层1 嵌入批注' if r['embed_markup'] else '层3 Zotero 阅读器' if r['zot_annots'] else '层2 疑似压平'] += 1
    outside = sum((1 for k in added if k not in old_all))
    lost = old_ann - new_ann
    L = ['', f'## 与 phase 2（{Path(path).name}）比对', '', f'- phase 2 的表只覆盖 75 个多附件条目的 {len(old_all)} 个附件；其中 annots>0 的 {len(old_ann)} 个，当时按 `annotated` 打标签的现有 {len(tagged)} 个', f'- 本次覆盖全部 {len(pdfs)} 个 PDF 附件，判定为“有批注”的 {len(new_ann)} 个', f'- **比 phase 2 多找出 {len(added)} 个**，其中 {outside} 个当时根本不在审核表里（单附件条目从未被检查过）', '- 多出来的按识别层分布：' + '；'.join((f'{k} {v}' for k, v in layer.most_common())), f'- phase 2 认为有批注、本次判干净的：{len(lost)}' + (f"（{', '.join(sorted(lost))}）" if lost and len(lost) <= 12 else ''), '']
    return '\n'.join(L) + '\n'

def summarize(rows: list[dict], reg: dict, date: str) -> str:
    by_item: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_item[r['item_key']].append(r)
    cat: dict[str, list] = defaultdict(list)
    n_items = len(reg)
    for ikey, d in reg.items():
        ck = d.get('citationKey') or ''
        rs = by_item.get(ikey, [])
        pdfs = [r for r in rs if r['ext'] == '.pdf' and r['exists']]
        if not pdfs:
            cat['no_pdf'].append((ck, ikey, ','.join((r['filename'] for r in rs)) or '(无存储附件)'))
            continue
        prim = [r for r in pdfs if r['role'] == 'primary']
        annot = [r for r in pdfs if r['role'] == 'annotated']
        others = [r for r in pdfs if r['role'] == 'other']
        if others:
            for r in others:
                cat['misnamed'].append((ck, ikey, r['att_key'], r['filename'], r['suggested_role'], r['suggested_name']))
        if len(pdfs) > 1 and (not prim) and others:
            cat['ambiguous'].append((ck, ikey, [r['filename'] for r in pdfs]))
        if not prim:
            cat['no_primary'].append((ck, ikey, [r['filename'] for r in pdfs]))
            continue
        p = prim[0]
        if p['embed_markup']:
            cat['primary_annotated'].append((ck, ikey, p['att_key'], p['embed_markup'], p['embed_types'], p['embed_pages'], p.get('embed_authors', '')))
        elif p['flat_marks']:
            cat['primary_flat_suspect'].append((ck, ikey, p['att_key'], p['flat_pages'], p['flat_marks'], p['flat_colors'], p['flat_examples']))
        if p['zot_annots']:
            cat['reader_on_primary'].append((ck, ikey, p['att_key'], p['zot_annots'], p['zot_annot_types']))
        if annot:
            cat['has_annotated'].append((ck, ikey, [r['filename'] for r in annot]))
        elif p['embed_markup']:
            cat['dirty_primary_no_annotated'].append((ck, ikey, p['att_key'], p['size_bytes']))
        else:
            cat['need_annotated_copy'].append((ck, ikey, p['att_key'], p['size_bytes'], bool(p['flat_marks'])))
    for r in rows:
        if r['zot_annots'] and r['role'] != 'primary':
            cat['reader_on_other'].append((r['citekey'], r['att_key'], r['role'], r['filename'], r['zot_annots']))
    gb = sum((x[3] for x in cat['need_annotated_copy'])) / 1024 ** 3
    ann_rows = [r for r in rows if r['ext'] == '.pdf']
    verd = Counter((r['verdict'] for r in ann_rows))
    L = [f'# 附件审计 {date}（只读）', '', f"常规条目 {n_items}；存储附件 {len(rows)}（PDF {len(ann_rows)}，磁盘上 {sum((1 for r in rows if r['exists']))}）", '', '## 批注识别（每个 PDF 附件一票，优先级 嵌入 > Zotero 阅读器 > 疑似压平）', '', *[f'- {k}: {v}' for k, v in verd.most_common()], '', f"- 层 1 嵌入批注的附件：{sum((1 for r in ann_rows if r['embed_markup']))}", f"- 层 2 疑似压平（无嵌入批注）：{sum((1 for r in ann_rows if r['flat_marks'] and (not r['embed_markup'])))}", f"- 层 3 Zotero 阅读器批注的附件：{sum((1 for r in ann_rows if r['zot_annots']))}（批注总数 {sum((r['zot_annots'] for r in ann_rows))}）", f"- Zotero `annotated` 标签的附件：{sum((1 for r in rows if 'annotated' in (r['att_tags'] or '')))}", f"- 阅读器痕迹（producer/creator）：{sum((1 for r in ann_rows if r['reader_hint']))}", '- 嵌入批注的署名分布（层 1，用来区分用户批注与出版商批注）：' + '；'.join((f'{k} {v}' for k, v in Counter((kv.split(':')[0] for r in ann_rows for kv in (r.get('embed_authors') or '').split(',') if kv)).most_common(8))), '', '## 条目分类', '', f'| 类别 | 条目数 | 动作 |', '|---|---|---|', f"| 有干净正本、缺批注本 | {len(cat['need_annotated_copy'])} | copy_as_annotated（约 {gb:.1f} GB） |", f"| 正本本身有嵌入批注、且还没有批注本 | {len(cat['dirty_primary_no_annotated'])} | 先 swap 再算批注本（需用户确认） |", f"| 正本本身有嵌入批注（含已有批注本的） | {len(cat['primary_annotated'])} | swap_annotated_primary（需用户确认） |", f"| 正本有疑似压平批注 | {len(cat['primary_flat_suspect'])} | 人工看 |", f"| 已有批注本 | {len(cat['has_annotated'])} | 无 |", f"| 无 PDF | {len(cat['no_pdf'])} | 无 |", f"| 有 PDF 但没有 `<ck>.pdf` 正本 | {len(cat['no_primary'])} | rename |", f"| 文件名不按键（附件数） | {len(cat['misnamed'])} | rename |", f"| 多 PDF 且无正本、角色难判 | {len(cat['ambiguous'])} | 需用户决定 |", f"| 阅读器批注挂在正本上 | {len(cat['reader_on_primary'])} | 可选 move_annotations |", '']

    def block(title: str, key: str, fmt, head: int=40) -> None:
        L.extend([f'## {title}（{len(cat[key])}）', ''])
        L.extend((fmt(x) for x in cat[key][:head]))
        if len(cat[key]) > head:
            L.append(f'- …其余 {len(cat[key]) - head} 条见 CSV')
        L.append('')
    block('正本本身有嵌入批注（现正本→_annotated，去批注副本作新正本）', 'primary_annotated', lambda x: f"- `{x[0]}` {x[1]}/{x[2]}：{x[3]} 处（{x[4]}），{x[5]} 页，署名 {x[6] or '(无)'}")
    block('正本有疑似压平批注（只是嫌疑，需人工看）', 'primary_flat_suspect', lambda x: f'- `{x[0]}` {x[1]}/{x[2]}：{x[3]} 页 {x[4]} 处 [{x[5]}] {x[6]}')
    block('文件名不按键（建议改名）', 'misnamed', lambda x: f"- `{x[0]}` {x[1]}/{x[2]}：`{x[3]}` → {x[4]}{(' `' + x[5] + '`' if x[5] else '（待定）')}")
    block('多 PDF 且无 `<ck>.pdf`，角色难判（需用户决定）', 'ambiguous', lambda x: f'- `{x[0]}` {x[1]}：' + ' | '.join((f'`{f}`' for f in x[2])))
    block('有 PDF 但没有正本', 'no_primary', lambda x: f'- `{x[0]}` {x[1]}：' + ' | '.join((f'`{f}`' for f in x[2])))
    block('无 PDF', 'no_pdf', lambda x: f'- `{x[0]}` {x[1]}：{x[2]}', head=30)
    block('Zotero 阅读器批注挂在正本上（若迁移批注本需 reparent）', 'reader_on_primary', lambda x: f'- `{x[0]}` {x[1]}/{x[2]}：{x[3]} 条（{x[4]}）')
    block('Zotero 阅读器批注挂在非正本附件上', 'reader_on_other', lambda x: f'- `{x[0]}` {x[1]} [{x[2]}] `{x[3]}`：{x[4]} 条')
    block('已有批注本', 'has_annotated', lambda x: f'- `{x[0]}`：' + ' | '.join((f'`{f}`' for f in x[2])), head=25)
    return '\n'.join(L) + '\n'
if __name__ == '__main__':
    os.environ.setdefault('PYTHONUTF8', '1')
    sys.exit(main())
