# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPORTS, add_common_args, zotero_data_dir
try:
    import fitz
except Exception:
    fitz = None
CLEAN_DIR = REPORTS / '_cache' / 'clean_pdf'
SPEC = REPORTS / 'attachments_normalize_spec.json'
TAG_CLEANED = 'primary-cleaned'
TAG_NO_PRIMARY = 'no-primary'
EXPLICIT_RENAMES: dict[str, dict[str, tuple[str, str]]] = {}

def strip_annotations(src: Path, dst: Path) -> tuple[bool, str]:
    if fitz is None:
        return (False, 'PyMuPDF missing')
    try:
        doc = fitz.open(src)
    except Exception as e:
        return (False, f'open: {str(e)[:60]}')
    removed, pages = (0, 0)
    try:
        pages = doc.page_count
        for page in doc:
            for a in list(page.annots() or []):
                try:
                    page.delete_annot(a)
                    removed += 1
                except Exception:
                    pass
        dst.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(dst), garbage=3, deflate=True)
    except Exception as e:
        return (False, f'save: {str(e)[:60]}')
    finally:
        doc.close()
    try:
        chk = fitz.open(dst)
    except Exception as e:
        return (False, f'verify-open: {str(e)[:60]}')
    try:
        left = 0
        for page in chk:
            left += len(list(page.annots() or []))
        npages = chk.page_count
    except Exception as e:
        return (False, f'verify: {str(e)[:60]}')
    finally:
        chk.close()
    if npages != pages:
        return (False, f'页数不符 {pages} -> {npages}')
    if left:
        return (False, f'仍有 {left} 处批注')
    return (True, f'removed {removed} annotations, {npages} pages OK')

def _strip_job(job: tuple[str, str, str]) -> tuple[str, bool, str]:
    ck, src, dst = job
    ok, note = strip_annotations(Path(src), Path(dst))
    return (ck, ok, note)

def role_after(r: dict) -> str:
    if r['role'] == 'other' and r['suggested_name']:
        return r['suggested_role']
    return r['role']

def free_annotated_name(ck: str, taken: set[str]) -> str:
    name = f'{ck}_annotated.pdf'
    n = 2
    while name in taken:
        name = f'{ck}_annotated{n}.pdf'
        n += 1
    return name

def build_actions(rows: list[dict], storage: Path, *, no_copy: bool=False, no_swap: bool=False, move_annotations: bool=False, make_clean: bool=False, clean_dir: Path=CLEAN_DIR, jobs: int=1, explicit: dict[str, dict[str, tuple[str, str]]] | None=None, clean_from_annotated: bool=True, tags: bool=True) -> tuple[list[dict], Counter, list[str], list[str]]:
    explicit = EXPLICIT_RENAMES if explicit is None else explicit
    by_item: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_item[r['item_key']].append(r)
    actions: list[dict] = []
    stats: Counter = Counter()
    skipped: list[str] = []
    clean_notes: list[str] = []
    clean_jobs: list[tuple[str, str, str]] = []
    final_state: dict[str, dict] = {}
    tags_now: dict[str, set[str]] = {}
    for ikey, rs in sorted(by_item.items(), key=lambda kv: kv[1][0]['citekey']):
        ck = rs[0]['citekey']
        tags_now[ikey] = {t for t in (rs[0].get('item_tags') or '').split(',') if t}
        if not ck:
            skipped.append(f'{ikey}: 无 citekey')
            continue
        pdfs = [r for r in rs if r['ext'] == '.pdf' and str(r['exists']) == 'True']
        if not pdfs:
            stats['items_no_pdf'] += 1
            continue
        for att_key, (role, tmpl) in explicit.get(ikey, {}).items():
            for r in pdfs:
                if r['att_key'] != att_key:
                    continue
                want = tmpl.format(ck=ck)
                r['suggested_role'] = role
                if r['filename'] == want:
                    r['role'], r['suggested_name'] = (role, '')
                else:
                    r['role'], r['suggested_name'] = ('other', want)
                stats['explicit_decision'] += 1
        for r in pdfs + [r for r in rs if r['ext'] != '.pdf' and str(r['exists']) == 'True']:
            if r['role'] == 'other' and r['suggested_name']:
                actions.append({'action': 'rename', 'itemKey': ikey, 'citekey': ck, 'attKey': r['att_key'], 'oldName': r['filename'], 'newName': r['suggested_name'], 'why': f'{r['role_basis']} → {r['suggested_role']}'})
                stats['rename'] += 1
            elif r['role'] == 'other':
                skipped.append(f'{ck} {r['att_key']} `{r['filename']}`：角色待用户定')
                stats['undecided_attachment'] += 1
        names_after = {r['suggested_name'] or r['filename'] for r in pdfs}
        prim = next((r for r in pdfs if role_after(r) == 'primary'), None)
        annot_rows = [r for r in pdfs if role_after(r) == 'annotated']
        has_annot = bool(annot_rows)
        if prim is None:
            stats['items_without_primary'] += 1
            if has_annot and clean_from_annotated:
                src = annot_rows[0]
                clean_path = clean_dir / f'{ck}.pdf'
                src_path = storage / src['att_key'] / src['filename']
                if make_clean:
                    clean_jobs.append((ck, str(src_path), str(clean_path)))
                actions.append({'action': 'attach_clean_primary', 'itemKey': ikey, 'citekey': ck, 'srcAttKey': src['att_key'], 'srcName': src['filename'], 'cleanPath': str(clean_path), 'newName': f'{ck}.pdf', 'title': ck, 'embedMarkup': int(src['embed_markup'] or 0), 'sizeBytes': int(src['size_bytes'] or 0), 'why': '本条无正本；用批注本的去批注副本作正本（批注本保持原名）'})
                stats['attach_clean_primary'] += 1
                stats['clean_bytes'] += int(src['size_bytes'] or 0)
                if tags:
                    actions.append({'action': 'add_tag', 'itemKey': ikey, 'citekey': ck, 'tag': TAG_CLEANED, 'why': '正本是去批注副本，不是出版商原件'})
                    stats['tag_primary_cleaned'] += 1
                final_state[ikey] = {'citekey': ck, 'names': names_after | {f'{ck}.pdf'}, 'no_primary': False}
            else:
                if tags:
                    actions.append({'action': 'add_tag', 'itemKey': ikey, 'citekey': ck, 'tag': TAG_NO_PRIMARY, 'why': '只有预印本/补充材料，没有正本；sync 退而取预印本'})
                    stats['tag_no_primary'] += 1
                skipped.append(f'{ck} {ikey}：无正本可用（' + ' | '.join((r['filename'] for r in pdfs)) + f'）→ 打标签 {TAG_NO_PRIMARY}')
                final_state[ikey] = {'citekey': ck, 'names': names_after, 'no_primary': True}
            continue
        embed = int(prim['embed_markup'] or 0)
        prim_zot = int(prim['zot_annots'] or 0)
        prim_name_after = prim['suggested_name'] or prim['filename']
        if embed:
            stats['primary_annotated'] += 1
            if not no_swap:
                annot_name = free_annotated_name(ck, {r['suggested_name'] or r['filename'] for r in pdfs} - {prim_name_after})
                clean_path = clean_dir / f'{ck}.pdf'
                if make_clean:
                    clean_jobs.append((ck, str(storage / prim['att_key'] / prim['filename']), str(clean_path)))
                actions.append({'action': 'swap_annotated_primary', 'itemKey': ikey, 'citekey': ck, 'attKey': prim['att_key'], 'oldName': prim['filename'], 'annotatedName': annot_name, 'cleanPath': str(clean_path), 'newName': f'{ck}.pdf', 'embedMarkup': embed, 'embedTypes': prim['embed_types'], 'zotAnnots': prim_zot, 'sizeBytes': int(prim['size_bytes'] or 0), 'why': '正本自带嵌入批注；现文件改名为批注本，去批注副本作新正本（无法还原真正的原件）'})
                stats['swap_annotated_primary'] += 1
                stats['clean_bytes'] += int(prim['size_bytes'] or 0)
                if tags:
                    actions.append({'action': 'add_tag', 'itemKey': ikey, 'citekey': ck, 'tag': TAG_CLEANED, 'why': '正本是去批注副本，不是出版商原件'})
                    stats['tag_primary_cleaned'] += 1
                if prim_zot:
                    stats['annots_follow_swap'] += 1
                    stats['annots_follow_swap_count'] += prim_zot
                final_state[ikey] = {'citekey': ck, 'names': names_after - {prim_name_after} | {annot_name, f'{ck}.pdf'}, 'no_primary': False}
            else:
                final_state[ikey] = {'citekey': ck, 'names': names_after, 'no_primary': False}
            continue
        if not has_annot and (not no_copy):
            annot_name = free_annotated_name(ck, names_after)
            actions.append({'action': 'copy_as_annotated', 'itemKey': ikey, 'citekey': ck, 'srcAttKey': prim['att_key'], 'srcPath': str(storage / prim['att_key'] / prim_name_after), 'srcNameAfter': prim_name_after, 'newName': annot_name, 'title': annot_name[:-4], 'sizeBytes': int(prim['size_bytes'] or 0), 'flatSuspect': int(prim['flat_marks'] or 0), 'why': '补批注本（正本副本，供用户批注）'})
            stats['copy_as_annotated'] += 1
            stats['copy_bytes'] += int(prim['size_bytes'] or 0)
            names_after = names_after | {annot_name}
        elif has_annot:
            stats['items_already_two'] += 1
            annot_name = annot_rows[0]['suggested_name'] or annot_rows[0]['filename']
        else:
            annot_name = f'{ck}_annotated.pdf'
        if move_annotations and prim_zot:
            actions.append({'action': 'move_annotations', 'itemKey': ikey, 'citekey': ck, 'fromAttKey': prim['att_key'], 'toName': annot_name, 'count': prim_zot, 'why': '阅读器批注随批注本迁移'})
            stats['move_annotations'] += 1
        final_state[ikey] = {'citekey': ck, 'names': names_after, 'no_primary': False}
    if make_clean and clean_jobs:
        clean_dir.mkdir(parents=True, exist_ok=True)
        todo = [(ck, s, d) for ck, s, d in clean_jobs if not Path(d).is_file()]
        clean_notes.append(f'（{len(clean_jobs)} 份，其中 {len(clean_jobs) - len(todo)} 份已在缓存里，跳过）')
        ok_n = fail_n = 0
        if todo:
            with cf.ProcessPoolExecutor(max_workers=max(1, jobs)) as ex:
                for ck, ok, note in ex.map(_strip_job, todo, chunksize=4):
                    if ok:
                        ok_n += 1
                    else:
                        fail_n += 1
                        clean_notes.append(f'FAIL {ck}: {note}')
        stats['clean_written_ok'] = ok_n
        stats['clean_written_fail'] = fail_n
        clean_notes.append(f'新写入 {ok_n} 份，失败 {fail_n} 份（校验：页数一致且 page.annots() 为空）')
        on_disk = [Path(d) for _, _, d in clean_jobs if Path(d).is_file()]
        stats['clean_on_disk'] = len(on_disk)
        stats['clean_actual_bytes'] = sum((p.stat().st_size for p in on_disk))
    bad_final = []
    for ikey, st in final_state.items():
        if st['no_primary']:
            stats['final_no_primary'] += 1
            continue
        ck = st['citekey']
        has_p = f'{ck}.pdf' in st['names']
        has_a = any((n.startswith(f'{ck}_annotated') and n.endswith('.pdf') for n in st['names']))
        if tags and has_p and (TAG_NO_PRIMARY in tags_now.get(ikey, ())):
            actions.append({'action': 'remove_tag', 'itemKey': ikey, 'citekey': ck, 'tag': TAG_NO_PRIMARY, 'why': '本条现在有正本了，摘掉 no-primary'})
            stats['untag_no_primary'] += 1
        if has_p and has_a:
            stats['final_two_ok'] += 1
        else:
            stats['final_incomplete'] += 1
            bad_final.append(f'{ck} {ikey}：缺 ' + ('正本 ' if not has_p else '') + ('批注本 ' if not has_a else '') + '（计划后：' + ' | '.join(sorted(st['names'])) + '）')
    skipped.extend(bad_final)
    stats['actions'] = len(actions)
    return (actions, stats, skipped, clean_notes)

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--audit', required=True, help='reports/attachments_audit_<date>.csv')
    ap.add_argument('--make-clean', action='store_true', help='also write the de-annotated PDFs to reports/_cache/clean_pdf/')
    ap.add_argument('--jobs', type=int, default=3, help='parallel PyMuPDF strippers for --make-clean (default 3)')
    ap.add_argument('--no-copy', action='store_true', help='skip the copy_as_annotated actions (only renames / swaps)')
    ap.add_argument('--no-tags', action='store_true', help='skip the add_tag actions (primary-cleaned / no-primary)')
    ap.add_argument('--swap', action='store_true', help='also emit swap_annotated_primary for primaries that already carry annotations (off by default: it needs a user decision, and the stripped copy is not the true original)')
    ap.add_argument('--move-annotations', action='store_true', help='also emit move_annotations for reader annotations that sit on a primary that stays the primary')
    ap.add_argument('--only-keys', default='', help='comma-separated citekeys, or @file with one per line')
    ap.add_argument('--out', default=str(SPEC))
    add_common_args(ap)
    a = ap.parse_args()
    only: set[str] = set()
    if a.only_keys:
        only = set(Path(a.only_keys[1:]).read_text(encoding='utf-8').split()) if a.only_keys.startswith('@') else {k.strip() for k in a.only_keys.split(',') if k.strip()}
    with open(a.audit, encoding='utf-8-sig') as f:
        rows = [r for r in csv.DictReader(f)]
    if only:
        rows = [r for r in rows if r['citekey'] in only]
    storage = zotero_data_dir() / 'storage'
    actions, stats, skipped, clean_notes = build_actions(rows, storage, no_copy=a.no_copy, no_swap=not a.swap, move_annotations=a.move_annotations, make_clean=a.make_clean and a.apply, jobs=a.jobs, tags=not a.no_tags)
    if a.limit:
        actions = actions[:a.limit]
    copy_gb = stats['copy_bytes'] / 1024 ** 3
    clean_gb = (stats['clean_actual_bytes'] or stats['clean_bytes']) / 1024 ** 3
    counts = Counter((x['action'] for x in actions))
    missing_clean = [x['citekey'] for x in actions if x.get('cleanPath') and (not Path(x['cleanPath']).is_file())]
    if missing_clean:
        stats['clean_missing'] = len(missing_clean)
        skipped.extend((f'{ck}：去批注副本还没生成（跑 --make-clean）' for ck in missing_clean[:20]))
    spec = {'generated': dt.datetime.now().isoformat(timespec='seconds'), 'source_audit': str(a.audit), 'actions': actions, 'stats': dict(stats) | {'copy_gb': round(copy_gb, 2), 'clean_gb': round(clean_gb, 2), 'actions': len(actions), 'by_action': dict(counts)}}
    L = [f'# 附件规范化规格 · {spec['generated']}', '', f'来源：`{a.audit}`', '', '## 动作数（按类型）', '', *[f'- {k}: {v}' for k, v in sorted(counts.items())], f'- **合计 {len(actions)}**', '', '## 统计', '', *[f'- {k}: {v}' for k, v in sorted(stats.items())], f'- 复制批注本预计新增：{copy_gb:.2f} GB', f'- 去批注副本预计新增：{clean_gb:.2f} GB（正本侧）', f'- **Zotero storage 预计新增合计：{copy_gb + clean_gb:.2f} GB**', '', f'## 需用户决定 / 跳过 / 最终状态不完整（{len(skipped)}）', ''] + [f'- {s}' for s in skipped[:80]]
    if len(skipped) > 80:
        L.append(f'- …其余 {len(skipped) - 80} 条')
    if clean_notes:
        L += ['', f'## 去批注副本（{len(clean_notes)} 条日志）', ''] + [f'- {s}' for s in clean_notes[:60]]
        if len(clean_notes) > 60:
            L.append(f'- …其余 {len(clean_notes) - 60} 条')
    text = '\n'.join(L) + '\n'
    if a.apply:
        Path(a.out).write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding='utf-8')
        (REPORTS / 'attachments_normalize_prep.md').write_text(text, encoding='utf-8')
        print(f'wrote {a.out} and reports/attachments_normalize_prep.md')
    else:
        print('(dry run: nothing written; add --apply)')
    print(text[:6000])
    return 0
if __name__ == '__main__':
    sys.exit(main())
