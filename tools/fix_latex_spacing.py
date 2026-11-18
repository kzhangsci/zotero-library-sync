# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
import argparse
import datetime as dt
import difflib
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPORTS
from library_paths import LIBRARY as LIB
TRASH = LIB / '.trash' / f'latex_spacing_{dt.date.today():%Y%m%d}'
FRONT = re.compile('---\\r?\\n.*?\\r?\\n---\\r?\\n?', re.S)
IMG_START = re.compile('^[ \\t>]*!\\[')
TABLE_START = re.compile('^[ \\t>]*<table', re.I)
DOLLAR = re.compile('(?<!\\\\)\\$')
PROSE = re.compile('(?<![\\\\A-Za-z])[A-Za-z]{4,}\\s+[A-Za-z]{4,}\\b')
IN_CMD = re.compile('(\\\\[A-Za-z]+)[ \\t]+(?=[{_^])')
IN_SUBSUP = re.compile('(?<!\\\\)[ \\t]*([_^])[ \\t]*')
IN_OPEN = re.compile('([{(\\[])[ \\t]+')
IN_CLOSE = re.compile('(?<!\\\\)[ \\t]+([})\\]])')
IN_DIGITS = re.compile('(?<=\\d)[ \\t]+(?=[\\d.])|(?<=\\d\\.)[ \\t]+(?=\\d)')
AFTER_PUNCT = re.compile('^[ \\t]+([,;:)\\]!?，。；：）、！？]|\\.(?!\\d))')
AFTER_HYPHEN = re.compile('^[ \\t]+-(?=[A-Za-z])')
BEFORE_BRACKET = re.compile('([(\\[（])[ \\t]+$')
BEFORE_SUPSUB = re.compile('(?<=[A-Za-z0-9)\\]}])[ \\t]+$')
DEGREE = re.compile('(?<=\\d)[ \\t]?◦')
COMMAND = re.compile('\\\\[A-Za-z]+')

def bare_script(c: str) -> bool:
    if c[:1] not in ('^', '_'):
        return False
    i = 0
    while i < len(c):
        if c[i] not in '^_':
            return False
        i += 1
        if i >= len(c):
            return False
        if c[i] == '{':
            depth = 0
            while i < len(c):
                depth += (c[i] == '{') - (c[i] == '}')
                if depth == 0:
                    break
                i += 1
            if depth:
                return False
            i += 1
        elif c[i] == '\\':
            m = COMMAND.match(c, i)
            if not m:
                return False
            i = m.end()
        else:
            i += 1
    return True

def split_math(s: str):
    idx = [m.start() for m in DOLLAR.finditer(s)]
    if not idx:
        return [(False, s)]
    if len(idx) % 2:
        return None
    parts, last = ([], 0)
    for a, b in zip(idx[0::2], idx[1::2]):
        parts += [(False, s[last:a]), (True, s[a + 1:b])]
        last = b + 1
    parts.append((False, s[last:]))
    if any((m and ('\n' in t or PROSE.search(t)) for m, t in parts)):
        return None
    return parts

def rebuild(parts) -> str:
    return ''.join((f'${t}$' if m else t for m, t in parts))

def squeeze(s: str) -> str:
    return re.sub('\\s+', '', s)

def fix_para(p: str) -> tuple[str, Counter, str]:
    c: Counter = Counter()
    s = p.lstrip('\n')
    lead = p[:len(p) - len(s)]
    if IMG_START.match(s):
        first, sep, rest = s.partition('\n')
        if not sep:
            return (p, c, '')
        new_rest, c, note = fix_para(rest)
        return (lead + first + sep + new_rest, c, note)
    if TABLE_START.match(s) or '$$' in s or '`' in s:
        return (p, c, '')
    parts = split_math(s)
    if parts is None:
        new, n = DEGREE.subn('°', s)
        if n:
            c['R6'] += n
        return (lead + new, c, '')
    for i, (m, t) in enumerate(parts):
        if not m:
            continue
        new = t
        for _ in range(4):
            before = new
            new = IN_CMD.sub('\\1', new)
            new = IN_SUBSUP.sub('\\1', new)
            new = IN_OPEN.sub('\\1', new)
            new = IN_CLOSE.sub('\\1', new)
            new = IN_DIGITS.sub('', new)
            if new == before:
                break
        if new != t:
            parts[i] = (True, new)
            c['R5'] += 1
    for i, (m, t) in enumerate(parts):
        if m:
            continue
        new = t
        if i > 0:
            mm = AFTER_PUNCT.match(new)
            if mm:
                new = mm.group(1) + new[mm.end():]
                c['R1'] += 1
            else:
                mm = AFTER_HYPHEN.match(new)
                if mm:
                    new = '-' + new[mm.end():]
                    c['R4'] += 1
        if i < len(parts) - 1:
            formula, after = (parts[i + 1][1], parts[i + 2][1])
            mm = BEFORE_BRACKET.search(new)
            if mm:
                new = new[:mm.start()] + mm.group(1)
                c['R2'] += 1
            elif bare_script(formula) and (not (formula[:1] == '^' and after[:1].isalpha())):
                mm = BEFORE_SUPSUB.search(new)
                if mm:
                    new = new[:mm.start()]
                    c['R3'] += 1
        parts[i] = (False, new)
    spaced = rebuild(parts)
    if squeeze(spaced) != squeeze(s) or '$$' in spaced:
        return (p, Counter(), 'reverted')
    for i, (m, t) in enumerate(parts):
        if not m:
            new, n = DEGREE.subn('°', t)
            if n:
                parts[i] = (False, new)
                c['R6'] += n
    return (lead + rebuild(parts), c, '')

def fix_text(text: str) -> tuple[str, Counter, list, int]:
    m = FRONT.match(text)
    head, body = (text[:m.end()], text[m.end():]) if m else ('', text)
    total: Counter = Counter()
    samples, reverted = ([], 0)
    out = []
    for chunk in re.split('(\\n[ \\t]*\\n)', body):
        if not chunk.strip() or re.fullmatch('\\n[ \\t]*\\n', chunk):
            out.append(chunk)
            continue
        new, c, note = fix_para(chunk)
        reverted += note == 'reverted'
        if new != chunk:
            total += c
            samples.append((c, chunk, new))
        out.append(new)
    return (head + ''.join(out), total, samples, reverted)

def context_diff(old: str, new: str, width: int=26) -> str:
    bits = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != 'equal':
            bits.append(f'`{old[max(0, i1 - width):i2 + width]}` → `{new[max(0, j1 - width):j2 + width]}`')
        if len(bits) >= 2:
            break
    return '; '.join(bits).replace('\n', '⏎')

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--only', default='', help='comma-separated citekeys')
    ap.add_argument('--zh', action='store_true', help='also fix the translations (_ZH.md): their quote blocks and formulas carry the same artefacts')
    ap.add_argument('--limit', type=int, default=0, help='stop after changing this many files (0 = all)')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(',') if x.strip()}
    pats = ['*_EN.md', '*_MD.md'] + (['*_ZH.md'] if a.zh else [])
    files = sorted((f for pat in pats for f in (LIB / 'papers').glob(f'*/{pat}') if not only or f.parent.name in only))
    by_kind = {k: Counter() for k in ('EN', 'MD', 'ZH')}
    files_changed = {k: 0 for k in by_kind}
    rule_samples: dict[str, list] = {}
    reverted_total = locked = changed = 0
    changed_list = []
    for f in files:
        if a.limit and changed >= a.limit:
            break
        kind = f.stem.rsplit('_', 1)[1]
        if any(f.parent.glob('.sync_*.lock')):
            locked += 1
            continue
        text = f.read_text(encoding='utf-8', errors='ignore')
        new, counts, samples, reverted = fix_text(text)
        reverted_total += reverted
        if new == text:
            continue
        changed += 1
        files_changed[kind] += 1
        by_kind[kind] += counts
        changed_list.append((f.name, sum(counts.values())))
        for c, old, nw in samples:
            for r in c:
                if len(rule_samples.setdefault(r, [])) < 8:
                    rule_samples[r].append((f.parent.name, context_diff(old, nw)))
        if a.apply:
            TRASH.mkdir(parents=True, exist_ok=True)
            if not (TRASH / f.name).exists():
                shutil.copy2(f, TRASH / f.name)
            f.write_text(new, encoding='utf-8')
    names = {'R1': '闭合$与标点间多空格', 'R2': '括号与开头$间多空格', 'R3': '纯上下标公式前多空格', 'R4': '闭合$与连字符间多空格', 'R5': '公式内部空格（渲染不可见）', 'R6': '◦ → °'}
    rep = [f"# 公式空格确定性修复 {('APPLY' if a.apply else 'DRY-RUN')} {dt.datetime.now():%Y-%m-%d %H:%M}", '', f"扫描 {len(files)} 份（{', '.join(pats)}），改动 {changed} 份：" + '、'.join((f'{k} {v}' for k, v in files_changed.items() if v)), f'跳过：有锁（worker 正在处理）{locked} 份；因不变量不满足而整段保持原样的段落 {reverted_total} 个', '', '| 规则 | 说明 | EN | MD | ZH |', '|---|---|---|---|---|']
    for r in ('R1', 'R2', 'R3', 'R4', 'R5', 'R6'):
        rep.append(f"| {r} | {names[r]} | {by_kind['EN'][r]} | {by_kind['MD'][r]} | {by_kind['ZH'][r]} |")
    rep += ['', '## 样例（每条规则最多 8 条）', '']
    for r in ('R1', 'R2', 'R3', 'R4', 'R5', 'R6'):
        rep.append(f'### {r} {names[r]}')
        rep += [f'- {ck}: {d}' for ck, d in rule_samples.get(r, [])] or ['- （无）']
        rep.append('')
    rep += ['## 改动文件', ''] + [f'- {n}（{k} 处规则命中）' for n, k in changed_list]
    out = REPORTS / f"latex_spacing_{dt.date.today():%Y%m%d}{('' if a.apply else '_dryrun')}.md"
    out.write_text('\n'.join(rep) + '\n', encoding='utf-8')
    print(f'scanned {len(files)}, changed {changed} ({files_changed}), locked {locked}, reverted paragraphs {reverted_total} -> {out.name}')
    for k in ('EN', 'MD', 'ZH'):
        if files_changed[k]:
            print(f'  {k}: ' + ', '.join((f'{r}={by_kind[k][r]}' for r in ('R1', 'R2', 'R3', 'R4', 'R5', 'R6'))))
    return 0
if __name__ == '__main__':
    sys.exit(main())
