# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_latex_spacing
import qc_glm
from common import REPORTS, add_common_args, year_of, zotero_data_dir, sha256
API = 'http://127.0.0.1:23119/api/users/0'
from library_paths import LIBRARY as LIB, GLOSSARY, MINERU_OUT
SUFFIXES = ('_ZH', '_EN', '_MD', '_Note')
CJK = re.compile('[一-鿿]')
KANA = re.compile('[\\u3041-\\u3096\\u30a1-\\u30fa]')
CACHE = REPORTS / '_cache'
SNAPSHOT = CACHE / 'zotero_snapshot.json'
FAIL_MEMO = CACHE / 'sync_failures.json'
CODEX_FLAG = CACHE / '_codex_exhausted.txt'
QC_TRASH = LIB / '.trash' / f'qc_glm_{dt.date.today().strftime('%Y%m%d')}'
STOP_FILE = CACHE / 'sync_stop.txt'
NO_TRANSLATE_LIST = REPORTS / 'sync_no_translate.txt'
PREPARSED_CSV = REPORTS / 'mineru_preparsed.csv'
FALLBACK_MODEL = 'bigmodel/glm-5.3-flash'
STALE_LOCK_HOURS = {'md': 2.0, 'zh': 12.0, 'qc': 6.0}
MAX_FAILS = 3

def api_all(path: str, params: dict | None=None) -> list:
    out, start = ([], 0)
    while True:
        q = {'limit': 100, 'start': start, **(params or {})}
        with urllib.request.urlopen(API + path + '?' + urllib.parse.urlencode(q), timeout=120) as r:
            batch, total = (json.load(r), int(r.headers.get('Total-Results', '0') or 0))
        out.extend(batch)
        start += len(batch)
        if not batch or start >= total:
            break
    return out

def load_zotero(log=print, snapshot_max_age: int=0) -> tuple[dict, dict, dict]:
    if snapshot_max_age > 0 and SNAPSHOT.is_file() and (time.time() - SNAPSHOT.stat().st_mtime < snapshot_max_age * 60):
        snap = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
        items, cols_raw = (snap['items'], snap['collections'])
        log(f'- Zotero snapshot from {snap.get('time')} (younger than {snapshot_max_age} min)')
        return _index_zotero(items, cols_raw)
    try:
        items = api_all('/items')
        cols_raw = api_all('/collections')
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            tmp = SNAPSHOT.with_suffix(f'.{os.getpid()}.tmp')
            tmp.write_text(json.dumps({'time': dt.datetime.now().isoformat(timespec='seconds'), 'items': items, 'collections': cols_raw}), encoding='utf-8')
            os.replace(tmp, SNAPSHOT)
        except OSError:
            pass
    except Exception as e:
        if not SNAPSHOT.is_file():
            raise
        snap = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
        log(f'!! Zotero API unavailable ({str(e)[:80]}); using snapshot from {snap.get('time')}')
        items, cols_raw = (snap['items'], snap['collections'])
    return _index_zotero(items, cols_raw)

def _index_zotero(items: list, cols_raw: list) -> tuple[dict, dict, dict]:
    cols = {c['key']: c['data'] for c in cols_raw if not c['data'].get('deleted')}
    reg = {x['key']: x['data'] | {'_parsedDate': (x.get('meta') or {}).get('parsedDate') or ''} for x in items if not x['data'].get('deleted') and x['data']['itemType'] not in ('attachment', 'note', 'annotation')}
    atts: dict[str, list] = defaultdict(list)
    for x in items:
        d = x['data']
        if not d.get('deleted') and d['itemType'] == 'attachment' and (d.get('parentItem') in reg):
            atts[d['parentItem']].append(d | {'key': x['key']})
    return (reg, atts, cols)

def col_path(cols: dict, key: str) -> str:
    parts = []
    while key:
        c = cols.get(key)
        if not c:
            break
        parts.append(c['name'])
        key = c.get('parentCollection') or ''
    return '/'.join(reversed(parts))

def primary_pdf(item_key: str, ck: str, atts: dict) -> Path | None:
    storage = zotero_data_dir() / 'storage'
    pdfs = [a for a in atts.get(item_key, []) if a.get('contentType') == 'application/pdf' and a.get('linkMode') in ('imported_file', 'imported_url') and a.get('filename')]
    if not pdfs:
        return None
    secondary = re.compile('_annotated|_preprint|_supplement|_lowres|_translation|_superseded_|moesm|\\besm\\b|supp|\\bsi\\.pdf$|supplementary', re.I)
    pdfs.sort(key=lambda a: (a['filename'] != f'{ck}.pdf', bool(secondary.search(a['filename'])), a['filename']))
    for a in pdfs:
        if a['filename'] != f'{ck}.pdf' and (secondary.search(a['filename']) or '_manuscript' in a['filename'].lower()):
            continue
        p = storage / a['key'] / a['filename']
        if p.is_file():
            return p
    return None

def front_matter(text: str) -> tuple[dict, str, str]:
    m = re.match('---\\r?\\n(.*?)\\r?\\n---\\r?\\n?', text, re.S)
    if not m:
        return ({}, '', text)
    fm = {}
    for line in m.group(1).splitlines():
        mm = re.match('^([A-Za-z_][\\w-]*):\\s*(.*)$', line)
        if mm:
            v = mm.group(2).strip()
            if v.startswith('"') and v.endswith('"'):
                try:
                    v = json.loads(v)
                except Exception:
                    v = v[1:-1]
            fm[mm.group(1)] = v
    return (fm, m.group(1), text[m.end():])

def yaml_block(fields: dict) -> str:
    lines = []
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f'{k}:')
            lines += [f'  - {json.dumps(x, ensure_ascii=False)}' for x in v]
        elif isinstance(v, str) and re.search('[:#\\"\'\\[\\]{}]', v):
            lines.append(f'{k}: {json.dumps(v, ensure_ascii=False)}')
        else:
            lines.append(f'{k}: {v}')
    return '---\n' + '\n'.join(lines) + '\n---\n'

def scan_library() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for folder in (LIB / 'papers').iterdir() if (LIB / 'papers').is_dir() else []:
        if not folder.is_dir():
            continue
        for md in folder.glob('*.md'):
            fm, _, _ = front_matter(md.read_text(encoding='utf-8', errors='ignore')[:4000])
            zk = fm.get('zotero_key')
            if zk:
                out[zk] = folder
                break
    return out

def creators_str(cr: list[dict]) -> str:
    return '; '.join(((c.get('lastName') or c.get('name') or '') + (', ' + c['firstName'] if c.get('firstName') else '') for c in cr))

def info_callout(d: dict, ck: str, key: str) -> str:
    pub = d.get('publicationTitle') or d.get('proceedingsTitle') or d.get('bookTitle') or d.get('university') or d.get('repository') or ''
    vol = ' '.join((x for x in (d.get('volume') and f'{d['volume']}', d.get('issue') and f'({d['issue']})', d.get('pages') and f'{d['pages']}') if x))
    doi = d.get('DOI') or ''
    return f'> [!info] 论文信息\n> **citekey**：{ck}\n> **题名**：{d.get('title', '')}\n> **作者**：{creators_str(d.get('creators', []))}\n> **年份 / 来源**：{year_of(d)} / {pub} {vol}\n> **DOI**：{doi}\n> **Zotero**：[打开条目](zotero://select/library/items/{key})\n'

def lock_path(folder: Path, step: str) -> Path:
    return folder / f'.sync_{step}.lock'

def acquire(folder: Path, step: str, worker: str) -> bool:
    folder.mkdir(parents=True, exist_ok=True)
    p = lock_path(folder, step)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f'{worker} {dt.datetime.now().isoformat(timespec='seconds')}'.encode('utf-8'))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age_h = (time.time() - p.stat().st_mtime) / 3600
            except FileNotFoundError:
                continue
            if age_h <= STALE_LOCK_HOURS.get(step, 2.0):
                return False
            try:
                p.unlink()
            except OSError:
                return False
    return False

def release(folder: Path, step: str, worker: str) -> None:
    p = lock_path(folder, step)
    try:
        if p.read_text(encoding='utf-8', errors='ignore').split(' ', 1)[0] == worker:
            p.unlink()
    except OSError:
        pass

def load_fails() -> dict:
    try:
        return json.loads(FAIL_MEMO.read_text(encoding='utf-8')) if FAIL_MEMO.is_file() else {}
    except (OSError, ValueError):
        return {}

def fail_count(fails: dict, ck: str, step: str) -> int:
    return int((fails.get(f'{ck}:{step}') or {}).get('count', 0))

def record_fail(ck: str, step: str, err: object) -> None:
    fails = load_fails()
    e = fails.get(f'{ck}:{step}') or {'count': 0}
    e.update({'count': int(e.get('count', 0)) + 1, 'last': str(err)[:300], 'time': dt.datetime.now().isoformat(timespec='seconds')})
    fails[f'{ck}:{step}'] = e
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = FAIL_MEMO.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(fails, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, FAIL_MEMO)
    except OSError:
        pass

def load_glossary() -> list[tuple[str, str]]:
    if not GLOSSARY.is_file():
        return []
    rows = []
    for line in GLOSSARY.read_text(encoding='utf-8', errors='ignore').splitlines():
        if line.startswith('|') and (not re.match('^\\|\\s*-', line)) and ('英文' not in line):
            cells = [c.strip() for c in line.strip('|').split('|')]
            if len(cells) >= 2 and cells[0] and cells[1]:
                rows.append((cells[0], cells[1]))
    return rows

def glossary_for(text: str, rows: list[tuple[str, str]], cap: int=150) -> str:
    low = text.lower()
    hits = []
    for en, zh in rows:
        terms = [t.strip().lower() for t in re.split('/|,|\\(|\\)', en) if len(t.strip()) >= 4]
        if any((t in low for t in terms)):
            hits.append(f'{en} → {zh}')
        if len(hits) >= cap:
            break
    return '\n'.join(hits)

class BackendError(RuntimeError):
    pass

class Backend:

    def __init__(self, model: str | None, codex_effort: str, log, claude_effort: str='high'):
        self.model, self.codex_effort, self.log, self.claude_effort = (model, codex_effort, log, claude_effort)
        self.used_run: set[str] = set()
        self.used_paper: set[str] = set()
        self.codex_fail = 0
        if self.is_codex and CODEX_FLAG.is_file():
            log(f'!! codex flagged exhausted ({CODEX_FLAG.name}); this run uses {FALLBACK_MODEL}')
            self.model = FALLBACK_MODEL

    @property
    def is_codex(self) -> bool:
        return bool(self.model and self.model.startswith('codex/'))

    @property
    def is_claude(self) -> bool:
        return bool(self.model and self.model.startswith('claude/'))

    def _mark(self, m: str) -> None:
        self.used_run.add(m)
        self.used_paper.add(m)

    def ask(self, prompt: str) -> str:
        if self.is_claude:
            from claude_client import ClaudeError, ask as claude_ask
            try:
                text, _ = claude_ask(prompt, self.model.split('/', 1)[1] or 'claude-opus-4-6', self.claude_effort)
            except ClaudeError as e:
                raise BackendError(str(e)) from e
            self._mark(self.model)
            return text
        if self.is_codex:
            from codex_client import CodexError, CodexQuotaError, ask as codex_ask
            try:
                text, _ = codex_ask(prompt, self.model.split('/', 1)[1] or 'gpt-6-astra', self.codex_effort)
                self.codex_fail = 0
                self._mark(self.model)
                return text
            except CodexQuotaError as e:
                CODEX_FLAG.parent.mkdir(parents=True, exist_ok=True)
                CODEX_FLAG.write_text(f'{dt.datetime.now().isoformat(timespec='seconds')} {e}', encoding='utf-8')
                self.log(f'!! codex quota exhausted -> rest of this run uses {FALLBACK_MODEL}: {str(e)[:200]}')
                self.model = FALLBACK_MODEL
            except CodexError as e:
                self.codex_fail += 1
                self.log(f'!! codex error #{self.codex_fail}: {str(e)[:200]}')
                if self.codex_fail < 3:
                    raise
                self.log(f'!! codex failing repeatedly -> rest of this run uses {FALLBACK_MODEL}')
                self.model = FALLBACK_MODEL
        from glm_client import ask
        text, _ = ask(prompt, model=self.model)
        self._mark(self.model or 'zcode-default')
        return text

def select_backends(args, log):
    steps = set(getattr(args, 'steps', 'qc,zh').split(','))
    roles = tuple((role for role in ('qc', 'zh') if role in steps))
    if not roles:
        return (None, None)
    if args.api_config:
        from model_api import create_backend, validate_config
        config = validate_config(json.loads(args.api_config.read_text(encoding='utf-8')), require_credentials=True, required_roles=roles)
        backends = {role: create_backend(config[role], role, log) for role in roles}
        log('- API backends: ' + ', '.join((f'{role}={backends[role].model}' for role in roles)) + '; no provider fallback')
        return (backends.get('qc'), backends.get('zh'))
    legacy = Backend(args.model, args.codex_effort, log, args.claude_effort)
    return (legacy if 'qc' in roles else None, legacy if 'zh' in roles else None)

def backend_stop_kind(backend):
    identity = getattr(backend, 'cache_identity', None)
    if isinstance(identity, dict) and identity.get('provider') in {'glm', 'deepseek', 'tongji'}:
        return identity['provider']
    return 'codex' if backend.is_codex else 'claude' if backend.is_claude else 'glm'

def split_paragraphs(body: str, max_chars: int=3500) -> list[list[str]]:
    paras = [p.strip() for p in re.split('\\n\\s*\\n', body) if p.strip()]
    chunks, cur, n = ([], [], 0)
    for p in paras:
        if n + len(p) > max_chars and cur:
            chunks.append(cur)
            cur, n = ([], 0)
        cur.append(p)
        n += len(p)
    if cur:
        chunks.append(cur)
    return chunks

def translate_chunk(paras: list[str], glossary: str, be: Backend) -> list[str]:
    numbered = '\n\n'.join((f'[{i + 1}]\n{p}' for i, p in enumerate(paras)))
    src = '日文' if len(KANA.findall(numbered)) >= 0.05 * max(1, len(numbered)) else '英文'
    prompt = f'你是物理海洋学与海洋地球物理领域的学术译者。把下面编号的{src}段落逐段译成简体中文，要求：\n1. 严格一一对应，输出与输入相同数量的段落，每段以同样的编号 [n] 开头，不合并、不拆分、不省略；\n2. 图片链接 ![](...)、公式（$...$ / $$...$$）、表格结构、引用标记（如 (Smith et al., 2020)、[12]）原样保留；图注和表注要译成中文（Fig. 1 译作 图 1）；Markdown 标题保留井号；\n3. 术语按下列对照表翻译，表外术语按学界通行译法，首次出现可括注英文；\n4. 只输出译文，不加解释。\n\n术语表：\n' + (glossary or '（无）') + f'\n\n{src}段落：\n' + numbered

    def parse(text: str) -> list[str]:
        markers = list(re.finditer('(?m)^[ \\t]*(?:\\[(\\d+)\\]|【(\\d+)】)[ \\t]*', text))
        if not markers:
            markers = list(re.finditer('(?m)^[ \\t]*(\\d+)[.)][ \\t]+', text))
        out = {}
        for i, marker in enumerate(markers):
            num = next((g for g in marker.groups() if g))
            end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
            out[int(num)] = text[marker.end():end].strip()
        res = [out.get(k + 1, '') for k in range(len(paras))]
        if any((not r for r in res)):
            blocks = [b.strip() for b in re.split('\\n\\s*\\n', text.strip()) if b.strip()]
            if len(blocks) == len(paras):
                res = blocks
        return res

    def parse_one(text: str) -> str:
        t = re.sub('(?m)^\\s*(?:\\[1\\]|【1】|1[.)]\\s)\\s*', '', text.strip(), count=1).strip()
        return '' if len(t) < 400 and STALL_RE.search(t) else t
    raw_dir = CACHE / 'glm_raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    last_err, bad_format, backend_errs = ('', False, 0)
    for attempt in range(3):
        p = prompt + ('\n\n（上一次回复没有按编号 [n] 逐段给出译文。附件/上文已经是全部待译文本，不要核对或读取任何文件，不要说稍后给出：现在就输出全部译文，严格每段以 [n] 开头，n 从 1 到 ' + str(len(paras)) + '。）' if bad_format else '')
        try:
            text = be.ask(p)
        except Exception as e:
            if getattr(e, 'stop_workflow', False):
                raise
            last_err = f'backend error (attempt {attempt + 1}): {str(e)[:200]}'
            backend_errs += 1
            continue
        (raw_dir / f'{time.time_ns()}_{os.getpid()}_{attempt}.txt').write_text(text, encoding='utf-8')
        res = parse(text)
        missing = [k + 1 for k, r in enumerate(res) if not r]
        src_len = sum((len(x) for x in paras))
        if not missing and len(text) < 400 and STALL_RE.search(text):
            missing = ['stall']
        if not missing:
            return res
        bad_format = True
        last_err = f'translation missing paragraphs {missing[:10]} (attempt {attempt + 1}, {len(text)} chars for {src_len} source chars)'
    if backend_errs >= 3:
        be.log(f'!! chunk of {len(paras)} paragraphs refused 3x by the backend ({last_err[:120]}); falling back to per-paragraph')
        out = []
        for p in paras:
            if p.startswith('![') or p.startswith('<table') or p.startswith('$$') or (len(p) < 20):
                out.append(p)
                continue
            try:
                one = be.ask(prompt.split(f'{src}段落：')[0] + f'{src}段落：\n[1]\n' + p)
                r1 = parse_one(one)
                out.append(r1 if r1 else '*（此段模型拒绝翻译，见上方原文）*')
            except Exception as e:
                be.log(f'   paragraph kept in English ({str(e)[:80]})')
                out.append('*（此段模型拒绝翻译，见上方原文）*')
        return out
    raise RuntimeError(last_err)
STALL_RE = re.compile('稍后|正在核对|正在处理|正在读取|请稍等|will provide|let me (check|read)|checking the', re.I)
IMG_LINK = re.compile('!\\[[^\\]]*\\]\\([^)]+\\)')
REF_HEADING = re.compile('^#{1,6}\\s*\\**\\s*(references?( and notes| cited)?|reference list|bibliography|literature cited|works cited|notes and references|参\\s*考\\s*文\\s*献|引\\s*用\\s*文\\s*献)\\s*\\**\\s*$', re.I | re.M)
NEXT_HEADING = re.compile('(?m)^#{1,6}\\s+\\S')
REF_LIKE = re.compile('references?|reference list|bibliography|literature cited|works cited|参\\s*考\\s*文\\s*献|引\\s*用\\s*文\\s*献|^#+\\s*notes\\s*$|further reading', re.I)

def split_reference_block(en_body: str) -> list[tuple[str, bool]]:
    m = REF_HEADING.search(en_body)
    if not m:
        return [(en_body, True)]
    segs = [(en_body[:m.start()], True)]
    pos = m.end()
    while True:
        nxt = NEXT_HEADING.search(en_body, pos)
        if not nxt:
            segs.append((en_body[m.start():], False))
            return segs
        line = en_body[nxt.start():en_body.find('\n', nxt.start()) if en_body.find('\n', nxt.start()) > 0 else len(en_body)]
        if REF_LIKE.search(line):
            pos = nxt.end()
            continue
        segs.append((en_body[m.start():nxt.start()], False))
        segs.append((en_body[nxt.start():], True))
        return segs

def build_zh(en_body: str, d: dict, ck: str, key: str, be: Backend, glossary_rows, *, parallel: int | None=None, chunk_cache: Path | bool | None=None) -> str:
    from concurrent.futures import ThreadPoolExecutor
    from glm_concurrency import positive_int, translate_cached
    workers = positive_int(parallel if parallel is not None else os.environ.get('ZH_PARALLEL') or 1, 'ZH_PARALLEL')
    if getattr(be, 'is_codex', False) or getattr(be, 'is_claude', False):
        workers = 1
    cache_dir = None if chunk_cache is False else Path(chunk_cache) if chunk_cache is not None else CACHE / 'zh_chunks'

    def run_chunk(paras):
        return translate_cached(paras, glossary_for('\n'.join(paras), glossary_rows), be, translate_chunk, cache_dir)
    out = [f'# {d.get('title', ck)}', '', info_callout(d, ck, key), '']
    for seg, translate in split_reference_block(en_body):
        if not translate:
            if seg.strip():
                out += ['', seg.strip(), '']
            continue
        seg = re.sub('(?m)^(!\\[[^\\n]*\\)[ \\t]*)\\n(?=\\S)', '\\1\\n\\n', seg)
        pairs = []
        groups = split_paragraphs(seg)
        if workers == 1 or len(groups) < 2:
            for paras in groups:
                pairs.extend(zip(paras, run_chunk(paras)))
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(groups))) as pool:
                for paras, zh in zip(groups, pool.map(run_chunk, groups)):
                    pairs.extend(zip(paras, zh))
        for en, zh in pairs:
            if en.startswith('#'):
                out += [zh if zh.startswith('#') else '#' + zh.lstrip('#'), '']
                continue
            if en.startswith('!['):
                out += [en, '']
                continue
            if en.startswith('$$') and en.rstrip().endswith('$$'):
                out += [en, '']
                continue
            lost = [m for m in IMG_LINK.findall(en) if m not in zh]
            if lost:
                zh = zh.rstrip() + '\n\n' + '\n\n'.join(lost)
            q = '\n'.join(('> ' + l for l in en.splitlines()))
            out += ['> [!quote]- en', q, '', zh, '']
    return '\n'.join(out)

def pause_boundary(apply, log):
    request = os.environ.get('LIBRARY_SYNC_PAUSE_FILE')
    if apply and request and Path(request).is_file():
        log('PAUSED: current step finished; no new step started')
        return True
    return False

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--collections', default='', help="comma-separated collection names or paths, e.g. 'SWOT,温盐阶梯,Eddy/0次表层涡旋' (sub-collections included; items are processed in this order); empty = whole library")
    ap.add_argument('--only', default='', help='single citekey')
    ap.add_argument('--only-list', default='', help='file of citekeys (one per line, # and blank lines ignored); restricts the run to them, like --only for many')
    ap.add_argument('--priority-list', default='', help='file of citekeys the qc step handles before the rest of its scope (same format as --only-list)')
    ap.add_argument('--steps', default='folders,md,qc,zh,note,indexes', help='user decision 2026-09-17: every new full text is proof-read (qc) before it is translated')
    ap.add_argument('--model', default='bigmodel/glm-5.3-flash', help="translation backend: zcode model (user decision 2026-09-10: glm-5.3-flash), 'codex/<model>' for the Codex CLI, or 'default' = zcode's configured main model")
    ap.add_argument('--api-config', type=Path, help='public GLM/DeepSeek per-role configuration; keys remain DPAPI-encrypted')
    ap.add_argument('--codex-effort', default='medium', help='reasoning effort for codex/<model> (translation does not need xhigh)')
    ap.add_argument('--claude-effort', default='high', help='effort for claude/<model> (Claude Code CLI: low, medium, high, xhigh, max)')
    ap.add_argument('--retry-failed', action='store_true', help='also take items that failed MAX_FAILS times (the fail memo is still updated); for a one-off retry with --only/--only-list')
    ap.add_argument('--mineru-lang', default='en')
    ap.add_argument('--fresh-source', action='store_true', help='ignore historical pre-parsed exports for explicitly replaced source PDFs')
    ap.add_argument('--worker-id', default=f'w{os.getpid()}', help='name written into per-item lock files')
    ap.add_argument('--require-qc', action='store_true', default=True, help='translate only full texts that carry the `qc:` marker (default since 2026-09-17; a paper proof-read in this run is translated right after)')
    ap.add_argument('--no-require-qc', dest='require_qc', action='store_false', help='translate full texts that were never proof-read (old behaviour)')
    ap.add_argument('--snapshot-max-age', type=int, default=0, help='minutes; reuse reports/_cache/zotero_snapshot.json when younger than this instead of reading the API (0 = always read the API)')
    ap.add_argument('--snapshot', type=Path, help='explicit verified snapshot for this job; no API fallback or shared-cache overwrite')
    add_common_args(ap)
    a = ap.parse_args()
    steps = set(a.steps.split(','))
    if a.model == 'default':
        a.model = None
    only_list = None
    if a.only_list:
        only_list = {x.strip() for x in Path(a.only_list).read_text(encoding='utf-8').splitlines() if x.strip() and (not x.lstrip().startswith('#'))}
    if a.only or only_list is not None or a.collections:
        steps.discard('indexes')
    priority = set()
    if a.priority_list:
        priority = {x.strip() for x in Path(a.priority_list).read_text(encoding='utf-8').splitlines() if x.strip() and (not x.lstrip().startswith('#'))}
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    log: list[str] = []
    L = lambda s: (log.append(s), print(s, flush=True))
    if pause_boundary(a.apply, L):
        return 75
    if a.snapshot:
        snap = json.loads(a.snapshot.read_text(encoding='utf-8'))
        if not isinstance(snap.get('items'), list) or not isinstance(snap.get('collections'), list):
            raise ValueError('invalid explicit Zotero snapshot')
        reg, atts, cols = _index_zotero(snap['items'], snap['collections'])
        L(f'- Explicit verified snapshot: {a.snapshot.name} ({snap.get('time')})')
    else:
        reg, atts, cols = load_zotero(L, a.snapshot_max_age)
    wanted, root_rank = (None, {})
    if a.collections:
        names = [n.strip() for n in a.collections.split(',') if n.strip()]
        root_rank = {}
        for k, c in cols.items():
            path = col_path(cols, k)
            for i, n in enumerate(names):
                if ('/' in n and path == n or ('/' not in n and c['name'] == n and (not c.get('parentCollection')))) and k not in root_rank:
                    root_rank[k] = i
        wanted = set(root_rank)
        changed = True
        while changed:
            changed = False
            for k, c in cols.items():
                p = c.get('parentCollection')
                if p in wanted and k not in wanted:
                    wanted.add(k)
                    root_rank[k] = root_rank[p]
                    changed = True
    targets = []
    for key, d in reg.items():
        ck = d.get('citationKey') or ''
        if not ck or d.get('deleted'):
            continue
        if a.only and ck != a.only:
            continue
        if only_list is not None and ck not in only_list:
            continue
        if wanted is not None and (not set(d.get('collections', [])) & wanted):
            continue
        rank = min((root_rank.get(c, 99) for c in d.get('collections', [])), default=99)
        targets.append((rank, key, d))
    targets.sort(key=lambda t: (t[0], t[2].get('citationKey', '')))
    rank_of = {k: r for r, k, _ in targets}
    targets = [(k, d) for _, k, d in targets]
    L(f'# sync {stamp}  mode={('APPLY' if a.apply else 'DRY-RUN')}  targets={len(targets)}  steps={sorted(steps)}  worker={a.worker_id}  model={('API profiles (see per-role backends)' if a.api_config else a.model or 'default')}' + (f'  only-list={Path(a.only_list).name} ({len(only_list)})' if only_list is not None else ''))
    folders = scan_library()
    glossary_rows = load_glossary() if 'zh' in steps else []
    skip_zh = {l.strip() for l in NO_TRANSLATE_LIST.read_text(encoding='utf-8').splitlines() if l.strip() and (not l.startswith('#'))} if NO_TRANSLATE_LIST.is_file() else set()
    fails = load_fails()
    stats = defaultdict(int)
    todo_md, todo_zh, todo_note, todo_qc, renames = ([], [], [], [], [])
    qc_after_md, zh_after_qc, md_done, qc_done = ({}, {}, [], [])
    skipped_fail = 0
    status_rows = []
    for key, d in targets:
        ck = d['citationKey']
        tags = {t['tag'] for t in d.get('tags', [])}
        folder = LIB / 'papers' / ck
        ttl = d.get('title') or ''
        lang_zh = (d.get('language') or '').lower().startswith('zh') or (bool(CJK.search(ttl)) and (not KANA.search(ttl))) or (folder / f'{ck}_MD.md').is_file()
        old = folders.get(key)
        if 'folders' in steps and old and (old != folder):
            renames.append((old, folder))
            if a.apply:
                if any(old.glob('.sync_*.lock')):
                    L(f'!! rename postponed {old.name} -> {ck}: a worker is busy in that folder')
                elif folder.exists():
                    L(f'!! cannot rename {old.name} -> {ck}: target exists')
                else:
                    try:
                        shutil.move(str(old), str(folder))
                        for f in folder.glob(f'{old.name}*.md'):
                            f.rename(folder / f.name.replace(old.name, ck, 1))
                        for f in folder.glob('*.md'):
                            t = f.read_text(encoding='utf-8')
                            t2 = t.replace(f'citekey: {old.name}', f'citekey: {ck}').replace(old.name + '_', ck + '_')
                            if t2 != t:
                                f.write_text(t2, encoding='utf-8')
                        folders[key] = folder
                    except OSError as e:
                        L(f'!! rename failed {old.name} -> {ck}: {e}')
        full_name = f'{ck}_MD.md' if lang_zh else f'{ck}_EN.md'
        has = {s: (folder / f'{ck}{s}.md').is_file() for s in SUFFIXES}
        has['_FULL'] = (folder / full_name).is_file()
        pdf = primary_pdf(key, ck, atts)
        qc_ok = True
        if ('qc' in steps or a.require_qc) and has['_FULL']:
            ffm, _, _ = front_matter((folder / full_name).read_text(encoding='utf-8', errors='ignore'))
            qc_ok = bool(ffm.get('qc'))
            if 'qc' in steps and (not qc_ok) and ('no-qc' not in tags):
                if fail_count(fails, ck, 'qc') >= MAX_FAILS and (not a.retry_failed):
                    skipped_fail += 1
                else:
                    todo_qc.append((key, d, full_name))
        if 'md' in steps and (not has['_FULL']) and ('no-md' not in tags) and ('no-pdf' not in tags) and pdf:
            if fail_count(fails, ck, 'md') >= MAX_FAILS and (not a.retry_failed):
                skipped_fail += 1
            else:
                todo_md.append((key, d, pdf, full_name))
                if 'qc' in steps and 'no-qc' not in tags and (fail_count(fails, ck, 'qc') < MAX_FAILS or a.retry_failed):
                    qc_after_md[key] = (key, d, full_name)
        zh_wanted = 'zh' in steps and (not lang_zh) and (not has['_ZH']) and ('no-translate' not in tags) and (ck not in skip_zh) and (has['_FULL'] or (key, d, pdf, full_name) in todo_md)
        if zh_wanted and fail_count(fails, ck, 'zh') >= MAX_FAILS and (not a.retry_failed):
            skipped_fail += 1
        elif zh_wanted and 'qc' in steps and a.require_qc and (not has['_FULL'] or not qc_ok) and ('no-qc' not in tags):
            zh_after_qc[key] = (key, d, full_name)
        elif zh_wanted and (qc_ok or not a.require_qc):
            todo_zh.append((key, d, full_name))
        if 'note' in steps and (not has['_Note']):
            todo_note.append((key, d))
        status_rows.append({'citekey': ck, 'zotero_key': key, 'pdf': bool(pdf), 'full': has['_FULL'], 'zh': has['_ZH'], 'note': has['_Note'], 'lang': 'zh' if lang_zh else 'en', 'collections': '; '.join((col_path(cols, c) for c in d.get('collections', []))), 'tags': '; '.join(sorted(tags))})
    if priority and todo_qc:
        todo_qc.sort(key=lambda t: t[1].get('citationKey') not in priority)
    if todo_md:
        from mineru_client import MAX_PAGES_PER_FILE, page_count
        todo_md.sort(key=lambda t: (page_count(t[2]) > MAX_PAGES_PER_FILE, rank_of.get(t[0], 99), t[1]['citationKey']))

    def _en_size(t):
        f = LIB / 'papers' / t[1]['citationKey'] / t[2]
        return f.stat().st_size if f.is_file() else 0
    todo_zh.sort(key=lambda t: (_en_size(t) > 400000, rank_of.get(t[0], 99), t[1]['citationKey']))
    L(f'- 文件夹改名：{len(renames)}' + (f'（例：{renames[0][0].name} → {renames[0][1].name}）' if renames else ''))
    L(f'- 待生成全文 md（MinerU）：{len(todo_md)}；待质检 QC：{len(todo_qc)}（其中全文生成后再质检：{len(qc_after_md)}）；待翻译 ZH：{len(todo_zh)}（质检通过后再翻译：{len(zh_after_qc)}）；待建 Note：{len(todo_note)}；因多次失败跳过：{skipped_fail}；本地不翻译名单：{len(skip_zh)}')
    for key, d, pdf, full_name in todo_md[:15]:
        L(f'  md: {full_name}  <- {pdf.name}')
    for key, d, full_name in todo_zh[:15]:
        L(f'  zh: {d['citationKey']}_ZH.md')
    for key, d, full_name in todo_qc[:15]:
        L(f'  qc: {full_name}' + ('  (priority)' if d.get('citationKey') in priority else ''))
    generated, locked, consec, aborted = (0, 0, 0, '')
    qc_be = be = None
    if a.apply and ('zh' in steps and (todo_zh or zh_after_qc) or ('qc' in steps and (todo_qc or qc_after_md))):
        qc_be, be = select_backends(a, L)
    if a.apply:
        for key, d in todo_note:
            if pause_boundary(a.apply, L):
                return 75
            ck = d['citationKey']
            folder = LIB / 'papers' / ck
            folder.mkdir(parents=True, exist_ok=True)
            if (folder / f'{ck}_Note.md').is_file():
                continue
            fm = yaml_block({'citekey': ck, 'zotero_key': key, 'doi': d.get('DOI') or '', 'title': d.get('title') or '', 'year': year_of(d), 'tags': ['paper'], 'created': dt.date.today().isoformat()})
            (folder / f'{ck}_Note.md').write_text(fm + f'\n# {ck}\n\n' + info_callout(d, ck, key) + '\n## 要点\n\n\n## 问题与想法\n\n\n## 关联文献\n\n', encoding='utf-8')
            stats['note'] += 1
        preparsed = {}
        if todo_md and PREPARSED_CSV.is_file():
            import csv as _csv2
            with open(PREPARSED_CSV, encoding='utf-8-sig', newline='') as f:
                preparsed = {r['zotero_key']: r for r in _csv2.DictReader(f) if r.get('md') and Path(r['md']).is_file()}
        if todo_md:
            from mineru_client import DUP_SIGNATURE, has_dup_artifact, parse_pdf_any as parse_pdf
        for key, d, pdf, full_name in todo_md:
            if pause_boundary(a.apply, L):
                return 75
            if a.limit and generated >= a.limit:
                break
            if STOP_FILE.is_file():
                L('- stop file present; ending run')
                aborted = 'stop'
                break
            ck = d['citationKey']
            folder = LIB / 'papers' / ck
            if (folder / full_name).is_file():
                continue
            if not acquire(folder, 'md', a.worker_id):
                locked += 1
                continue
            try:
                if (folder / full_name).is_file():
                    continue
                pre = None if a.fresh_source else preparsed.get(key)
                parse_name = ck + ('_source_' + sha256(pdf)[:16] if a.fresh_source else '')
                src_tag = 'mineru'
                try:
                    if pre:
                        md = Path(pre['md'])
                        src_tag = f'mineru (pre-parsed {pre['method']} {pre['score']}: {md})'
                    else:
                        md = parse_pdf(pdf, MINERU_OUT / parse_name, a.mineru_lang if not (d.get('language') or '').startswith('zh') else 'ch')
                except Exception as e:
                    transient = bool(re.search('API error|HTTP|timeout|Connection|429|upload failed|URLError|RemoteDisconnected|Max retries', str(e), re.I))
                    L(f'!! MinerU failed {ck}: {e}' + ('  [transient, not memoized]' if transient else ''))
                    stats['md_failed'] += 1
                    if not transient:
                        record_fail(ck, 'md', e)
                    consec += 1
                    if consec >= MAX_FAILS:
                        L(f'!! {consec} consecutive MinerU failures, aborting run')
                        aborted = 'md'
                        break
                    continue
                body = md.read_text(encoding='utf-8', errors='ignore')
                if not pre and (has_dup_artifact(body) >= DUP_SIGNATURE[1] or len(re.findall('[\\x01-\\x05]', body)) > 5):
                    try:
                        md = parse_pdf(pdf, MINERU_OUT / (parse_name + '_ocr'), a.mineru_lang if not (d.get('language') or '').startswith('zh') else 'ch', ocr=True)
                        body = md.read_text(encoding='utf-8', errors='ignore')
                        L(f'  md: {ck} re-parsed with OCR (broken PDF text layer: sub/sup duplication or symbol-font control characters)')
                    except Exception as e:
                        L(f'!! OCR re-parse failed {ck}, keeping the text-layer version: {e}')
                asset_rel = 'assets' + ('/source_' + sha256(pdf)[:16] if a.fresh_source else '')
                (folder / asset_rel).mkdir(parents=True, exist_ok=True)
                img_dir = Path(pre['images']) if pre and pre.get('images') else md.parent / 'images'
                for img in img_dir.glob('*') if img_dir.is_dir() else []:
                    shutil.copy2(img, folder / asset_rel / img.name)
                body = re.sub('\\]\\(images/', '](' + asset_rel + '/', body)
                n_cjk = len(CJK.findall(body))
                if full_name.endswith('_EN.md') and n_cjk > 0.25 * max(1, n_cjk + len(re.findall('[A-Za-z]', body))) and (len(KANA.findall(body)) < 5):
                    full_name = f'{ck}_MD.md'
                    L(f'  md: {ck} body is Chinese -> {full_name} (set language=zh in Zotero to make this explicit)')
                fm = yaml_block({'citekey': ck, 'zotero_key': key, 'doi': d.get('DOI') or '', 'title': d.get('title') or '', 'source': src_tag, 'source_pdf': pdf.name, 'source_sha256': sha256(pdf), 'generated': dt.date.today().isoformat()})
                body = fix_latex_spacing.fix_text(body)[0]
                (folder / full_name).write_text(fm + '\n' + body, encoding='utf-8')
                stats['md'] += 1
                md_done.append(key)
                generated += 1
                consec = 0
                L(f'  md done: {full_name}' + ('  (pre-parsed)' if pre else ''))
            finally:
                release(folder, 'md', a.worker_id)
        for key in md_done:
            if key in qc_after_md:
                todo_qc.append(qc_after_md[key])
        consec = 0
        for key, d, full_name in todo_qc:
            if pause_boundary(a.apply, L):
                return 75
            if aborted or (a.limit and generated >= a.limit):
                break
            kind_stop = CACHE / f'sync_stop_{backend_stop_kind(qc_be)}.txt'
            if STOP_FILE.is_file() or kind_stop.is_file():
                L(f'- stop file present ({(STOP_FILE if STOP_FILE.is_file() else kind_stop).name}); ending run')
                aborted = 'stop' if STOP_FILE.is_file() else 'stop_kind'
                break
            ck = d['citationKey']
            folder = LIB / 'papers' / ck
            src = folder / full_name
            if not src.is_file():
                continue
            if not acquire(folder, 'qc', a.worker_id):
                locked += 1
                continue
            try:
                text = src.read_text(encoding='utf-8', errors='ignore')
                ffm, _, body = front_matter(text)
                if ffm.get('qc'):
                    continue
                t0 = time.time()
                pdf = primary_pdf(key, ck, atts)
                try:
                    pdf_text = qc_glm.pdf_norm(pdf) if pdf else ''
                except Exception as e:
                    pdf_text = ''
                    L(f'  qc: {ck} PDF unreadable ({str(e)[:80]})')
                if not pdf_text:
                    L(f'  qc: {ck} has no usable PDF text layer; PDF guard off for this paper')
                try:
                    qc_be.used_paper.clear()
                    if a.api_config and pdf:
                        cache_dir = CACHE / 'qc_chunks' / sha256(pdf)
                        new_body, accepted, notes = qc_glm.qc_body(body, qc_be, pdf_text, cache_dir)
                    else:
                        new_body, accepted, notes = qc_glm.qc_body(body, qc_be, pdf_text)
                    unanswered = [x for x in notes if x.startswith('backend error')]
                    if unanswered:
                        raise RuntimeError(f'{len(unanswered)} chunk(s) got no answer from the backend: {unanswered[0][:120]}')
                except Exception as e:
                    L(f'!! qc failed {ck}: {e}')
                    stats['qc_failed'] += 1
                    if not getattr(e, 'stop_workflow', False):
                        record_fail(ck, 'qc', e)
                    consec += 1
                    if consec >= MAX_FAILS:
                        L(f'!! {consec} consecutive qc failures, aborting run')
                        aborted = 'qc'
                        break
                    continue
                if src.read_text(encoding='utf-8', errors='ignore') != text:
                    L(f'!! qc discarded {ck}: {full_name} changed while it was being proof-read; left for a later run')
                    stats['qc_discarded'] += 1
                    continue
                qc_backup = QC_TRASH / (str(ffm.get('source_sha256'))[:16] if a.fresh_source else '')
                if new_body != body and (not (qc_backup / src.name).exists()):
                    qc_backup.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, qc_backup / src.name)
                ffm['qc'] = f'{'+'.join(sorted(qc_be.used_paper)) or qc_be.model} {dt.date.today().isoformat()}'
                src.write_text(yaml_block(ffm) + '\n' + new_body, encoding='utf-8')
                stats['qc'] += 1
                qc_done.append(key)
                stats['qc_fixes'] += len(accepted)
                rejected_count = qc_glm.rejection_count(notes)
                stats['qc_rejected'] += rejected_count
                stats['qc_pdf_rejected'] += len([x for x in notes if x.startswith('rejected [pdf]')])
                generated += 1
                consec = 0
                L(f'  qc done: {full_name}  改 {len(accepted)} 处，拒 {rejected_count} 处，{int(time.time() - t0)}s')
                for x in accepted[:6]:
                    L(f'     + {x[:150]}')
                for x in notes:
                    L(f'     - {x}')
            finally:
                release(folder, 'qc', a.worker_id)
        for key in qc_done:
            if key in zh_after_qc:
                todo_zh.append(zh_after_qc[key])
        consec = 0
        for key, d, full_name in todo_zh:
            if pause_boundary(a.apply, L):
                return 75
            if aborted:
                break
            if a.limit and generated >= a.limit:
                break
            kind_stop = CACHE / f'sync_stop_{backend_stop_kind(be)}.txt'
            if STOP_FILE.is_file() or kind_stop.is_file():
                L(f'- stop file present ({(STOP_FILE if STOP_FILE.is_file() else kind_stop).name}); ending run')
                aborted = 'stop' if STOP_FILE.is_file() else 'stop_kind'
                break
            ck = d['citationKey']
            folder = LIB / 'papers' / ck
            src = folder / full_name
            if not src.is_file() or (folder / f'{ck}_ZH.md').is_file():
                continue
            if not acquire(folder, 'zh', a.worker_id):
                locked += 1
                continue
            try:
                if (folder / f'{ck}_ZH.md').is_file():
                    continue
                src_text = src.read_text(encoding='utf-8', errors='ignore')
                _, _, body = front_matter(src_text)
                be.used_paper.clear()
                t0 = time.time()
                try:
                    zh = build_zh(body, d, ck, key, be, glossary_rows)
                except Exception as e:
                    L(f'!! translation failed {ck}: {e}' + ('  [backend error]' if isinstance(e, BackendError) else ''))
                    stats['zh_failed'] += 1
                    if not getattr(e, 'stop_workflow', False):
                        record_fail(ck, 'zh', e)
                    consec += 1
                    if consec >= MAX_FAILS:
                        L(f'!! {consec} consecutive translation failures, aborting run')
                        aborted = 'zh'
                        break
                    continue
                fm = yaml_block({'citekey': ck, 'zotero_key': key, 'doi': d.get('DOI') or '', 'title': d.get('title') or '', 'source': 'translated by ' + '+'.join(sorted(be.used_paper)), 'generated': dt.date.today().isoformat()})
                zh = fix_latex_spacing.fix_text(zh)[0]
                if src.read_text(encoding='utf-8', errors='ignore') != src_text:
                    L(f'!! zh {ck}: {full_name} changed while it was being translated; {ck}_ZH.md written from the earlier text, check its quote blocks')
                    stats['zh_source_changed'] += 1
                (folder / f'{ck}_ZH.md').write_text(fm + '\n' + zh, encoding='utf-8')
                stats['zh'] += 1
                generated += 1
                consec = 0
                L(f'  zh done: {ck}_ZH.md  ({'+'.join(sorted(be.used_paper))}, {int(time.time() - t0)}s)')
                if not a.api_config and a.model and a.model.startswith('codex/') and (not be.is_codex):
                    L('- codex worker fell back to GLM; ending this run so the runner can retry Codex when its quota returns')
                    aborted = 'codex_fallback'
                    break
            finally:
                release(folder, 'zh', a.worker_id)
    if 'indexes' in steps and a.apply:
        idx = LIB / 'indexes'
        (idx / 'by-collection').mkdir(parents=True, exist_ok=True)
        import csv as _csv
        with open(idx / 'status.csv', 'w', newline='', encoding='utf-8-sig') as f:
            w = _csv.DictWriter(f, fieldnames=list(status_rows[0].keys()))
            w.writeheader()
            w.writerows(status_rows)

        def line(r):
            d = reg[r['zotero_key']]
            ck = r['citekey']
            folder = LIB / 'papers' / ck
            links = []
            if (folder / f'{ck}_ZH.md').is_file():
                links.append(f'[[papers/{ck}/{ck}_ZH|译文]]')
            full = f'{ck}_MD.md' if r['lang'] == 'zh' else f'{ck}_EN.md'
            if (folder / full).is_file():
                links.append(f'[[papers/{ck}/{full[:-3]}|全文]]')
            return f'- [[papers/{ck}/{ck}_Note|{ck}]] {(d.get('title') or '')[:90]} ({year_of(d)})' + (' · ' + ' · '.join(links) if links else '')
        (idx / 'all.md').write_text('# 全部文献\n\n' + '\n'.join((line(r) for r in status_rows)) + '\n', encoding='utf-8')
        by_col, by_year, by_journal, by_tag = (defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list))
        for r in status_rows:
            d = reg[r['zotero_key']]
            for c in d.get('collections', []):
                by_col[col_path(cols, c)].append(r)
            by_year[year_of(d) or '无年份'].append(r)
            by_journal[d.get('publicationTitle') or d.get('proceedingsTitle') or d.get('itemType')].append(r)
            for t in r['tags'].split('; '):
                if t:
                    by_tag[t].append(r)
        for name, rows in by_col.items():
            safe = re.sub('[\\\\/:*?\\"<>|]', '_', name)
            (idx / 'by-collection' / f'{safe}.md').write_text(f'# {name}\n\n' + '\n'.join((line(r) for r in rows)) + '\n', encoding='utf-8')
        for fn, groups in (('by-year.md', by_year), ('by-journal.md', by_journal), ('by-tag.md', by_tag)):
            parts = [f'# {fn[3:-3]}', '']
            for g in sorted(groups):
                parts += [f'## {g}（{len(groups[g])}）', ''] + [line(r) for r in groups[g]] + ['']
            (idx / fn).write_text('\n'.join(parts), encoding='utf-8')
        L(f'- 索引已写：all.md, by-collection/{len(by_col)}, by-year, by-journal, by-tag, status.csv')
    used_models = (be.used_run if be else set()) | (qc_be.used_run if qc_be else set())
    if a.api_config:
        for role, backend in (('qc', qc_be), ('zh', be)):
            history = getattr(backend, 'usage_history', ())
            if history:
                metrics = {'role': role, 'requests': len(history), 'models': sorted({x['model'] for x in history}), 'total_tokens': sum((x['usage']['total_tokens'] for x in history)) if all(('total_tokens' in x.get('usage', {}) for x in history)) else None, 'elapsed_request_seconds': round(sum((x['elapsed_seconds'] for x in history)), 3)}
                L('API_USAGE ' + json.dumps(metrics, ensure_ascii=False))
    L(f'SUMMARY {json.dumps(dict(stats), ensure_ascii=False)} pending_md={len(todo_md)} pending_qc={len(todo_qc)} pending_zh={len(todo_zh)} skipped_fail={skipped_fail} locked={locked} aborted={aborted or 'none'} used={'+'.join(sorted(used_models)) or '-'}')
    (REPORTS / f'sync_{stamp}.md').write_text('\n'.join(log) + '\n', encoding='utf-8')
    return 0
if __name__ == '__main__':
    sys.exit(main())
