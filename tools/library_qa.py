# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import argparse
from collections import Counter
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import html
import json
from pathlib import Path, PureWindowsPath
import re
import sys
from urllib.parse import unquote, urlsplit
import uuid
from library_paths import REPO, DATA, LIBRARY, REPORTS
REF_HEADING = re.compile('^#{1,6}\\s*\\**\\s*(references?( and notes| cited)?|reference list|bibliography|literature cited|works cited|notes and references|参\\s*考\\s*文\\s*献|引\\s*用\\s*文\\s*献)\\s*\\**\\s*$', re.I | re.M)
NEXT_HEADING = re.compile('(?m)^#{1,6}\\s+\\S')
REF_LIKE = re.compile('references?|reference list|bibliography|literature cited|works cited|参\\s*考\\s*文\\s*献|引\\s*用\\s*文\\s*献|^#+\\s*notes\\s*$|further reading', re.I)
QUOTE = '> [!quote]- en'
CJK = re.compile('[一-鿿]')
KANA = re.compile('[\\u3041-\\u3096\\u30a1-\\u30fa]')
IMAGE = re.compile('!\\[[^\\]\\n]*\\]\\(\\s*(<[^>\\n]+>|(?:[^()\\s]|\\([^()]*\\))+)(?:\\s+"[^"\\n]*")?\\s*\\)')
WIKI_IMAGE = re.compile('!\\[\\[([^\\]\\n]+)\\]\\]')
HTML_IMAGE = re.compile('<img\\b[^>]*\\bsrc\\s*=\\s*[\\"\\x27]([^\\"\\x27]+)[\\"\\x27]', re.I)
DISPLAY = re.compile('(?<!\\\\)\\$\\$.*?(?<!\\\\)\\$\\$|\\\\\\[.*?\\\\\\]|\\\\begin\\{(equation\\*?|align\\*?|gather\\*?|multline\\*?)\\}.*?\\\\end\\{\\1\\}', re.S)
MARKER = re.compile('^\\s*(?:#{1,6}\\s*)?(\\[\\d+\\]|【\\d+】|\\d+[.)])(?:[ \\t]*(.*))?$')
LABEL_LINE = re.compile('\\*{0,2}\\([a-z0-9]{1,4}(?:\\s*[,，]\\s*[a-z0-9]{1,4})*\\)\\*{0,2}\\.?')
PLACEHOLDER = re.compile("此段模型拒绝翻译|(?:无法|不能|拒绝|未能)(?:提供)?(?:此段|该段|这段|全文|该内容|此内容)?(?:的)?翻译|translation (?:failed|missing paragraphs)|missing paragraphs|(?:unable|cannot|can't|refuse) to translate|\\[translation failed\\]", re.I)
STALL = re.compile('(?:稍后|待补充|待翻译|正在核对|正在处理|正在读取|请稍等)|will (?:provide|translate)|let me (?:check|read)|checking the', re.I)
SEMANTIC_LIMIT = '自动 QA 仅检查元信息、文件和结构覆盖；非空译文仍可能漏译句子、错译术语、数值、否定或因果关系。不能保证科学正确，不能替代原 QC 护栏、PDF 对照和人工语义审阅；未调用 LLM。'

def _issue(issues: list, code: str, message: str, **where) -> None:
    issues.append({'code': code, 'message': message, **where})

def _norm(text: str) -> str:
    return re.sub('\\s+', ' ', text).strip()

def _front_matter(text: str) -> tuple[dict, str, list[str]]:
    match = re.match('---\\r?\\n(.*?)\\r?\\n---\\r?\\n?', text.lstrip('\ufeff'), re.S)
    if not match:
        return ({}, text, ['缺失或未闭合 YAML front matter'])
    fields, problems = ({}, [])
    for line in match[1].splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        pair = re.match('^([A-Za-z_][\\w-]*):\\s*(.*)$', line)
        if not pair:
            if not re.match('^\\s+-\\s', line):
                problems.append(f'不支持或损坏的 YAML 行：{line[:100]}')
            continue
        key, value = pair.groups()
        value = value.strip()
        if key in fields:
            problems.append(f'重复 YAML 字段：{key}')
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                problems.append(f'损坏的 YAML 字符串：{key}')
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        elif value in ('|', '>', '|-', '>-'):
            problems.append(f'需人工检查多行 YAML 字段：{key}')
        fields[key] = value
    return (fields, text.lstrip('\ufeff')[match.end():], problems)

def _reference_segments(body: str) -> list[tuple[str, bool]]:
    match = REF_HEADING.search(body)
    if not match:
        return [(body, True)]
    segments, pos = ([(body[:match.start()], True)], match.end())
    while True:
        nxt = NEXT_HEADING.search(body, pos)
        if not nxt:
            return segments + [(body[match.start():], False)]
        end = body.find('\n', nxt.start())
        line = body[nxt.start():end if end > 0 else len(body)]
        if REF_LIKE.search(line):
            pos = nxt.end()
            continue
        return segments + [(body[match.start():nxt.start()], False), (body[nxt.start():], True)]

def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split('\\n\\s*\\n', text) if p.strip()]

def image_only_line(line: str) -> bool:
    stripped = line.strip()
    if IMAGE.fullmatch(stripped) or WIKI_IMAGE.fullmatch(stripped):
        return True
    return not re.sub('\\s+', '', HTML_IMAGE.sub('', stripped))

def image_only_paragraph(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and all((image_only_line(line) for line in lines))

def label_image_paragraph(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    labels = [line for line in lines if not image_only_line(line)]
    return bool(labels) and len(labels) < len(lines) and all((LABEL_LINE.fullmatch(line.strip()) for line in labels))

def _kind(text: str) -> str:
    if text.startswith('#'):
        return 'heading'
    if text.startswith('!['):
        return 'image'
    if text.startswith('$$') and text.rstrip().endswith('$$'):
        return 'formula'
    if label_image_paragraph(text):
        return 'label_image'
    return 'quote'

def _expected_units(body: str) -> list[dict]:
    units = []
    for segment, translate in _reference_segments(body):
        if not translate:
            if segment.strip():
                units.append({'kind': 'references', 'text': segment.strip()})
            continue
        segment = re.sub('(?m)^(!\\[[^\\n]*\\)[ \\t]*)\\n(?=\\S)', '\\1\\n\\n', segment)
        units.extend(({'kind': _kind(p), 'text': p} for p in _paragraphs(segment)))
    return units

def _actual_units(body: str, title: str, errors: list, file: str) -> list[dict]:
    paragraphs = _paragraphs(body)
    if paragraphs and paragraphs[0] == f'# {title}':
        paragraphs.pop(0)
    else:
        _issue(errors, 'zh_preamble', 'ZH 缺少 build_zh 的题名头；无法把它当作正文标题。', file=file)
    if paragraphs and paragraphs[0].startswith('> [!info]'):
        paragraphs.pop(0)
    else:
        _issue(errors, 'zh_preamble', 'ZH 缺少论文信息 callout。', file=file)
    units = []
    for segment, translate in _reference_segments('\n\n'.join(paragraphs)):
        if not translate:
            if segment.strip():
                units.append({'kind': 'references', 'text': segment.strip()})
            continue
        parts, i = (_paragraphs(segment), 0)
        while i < len(parts):
            paragraph = parts[i]
            if paragraph.splitlines()[0] == QUOTE:
                lines = paragraph.splitlines()[1:]
                if not lines or any((not line.startswith('>') for line in lines)):
                    _issue(errors, 'quote_format', '英文 quote 格式损坏或内容为空。', file=file, unit=len(units) + 1)
                en = '\n'.join((re.sub('^> ?', '', line) for line in lines))
                source = set(map(_image_key, _images(en)))
                blocks, took_prose = ([], False)
                while i + 1 < len(parts) and (not parts[i + 1].startswith('>')):
                    nxt = parts[i + 1]
                    images = list(map(_image_key, _images(nxt)))
                    if _kind(nxt) == 'quote' and (not took_prose):
                        i += 1
                        blocks.append(nxt)
                        took_prose = True
                        continue
                    if images and all((t in source for t in images)) and (image_only_paragraph(nxt) or not blocks):
                        i += 1
                        blocks.append(nxt)
                        continue
                    break
                translation = '\n\n'.join(blocks)
                if label_image_paragraph(en) and _norm(translation) == _norm(en):
                    units.append({'kind': 'label_image', 'text': translation})
                else:
                    units.append({'kind': 'quote', 'text': en, 'translation': translation})
            else:
                kind = _kind(paragraph)
                units.append({'kind': 'orphan' if kind == 'quote' else kind, 'text': paragraph})
            i += 1
    return units

def _images(text: str) -> list[str]:
    return [m[1].strip('<>') for m in IMAGE.finditer(text)] + [m[1].split('|', 1)[0] for m in WIKI_IMAGE.finditer(text)] + [html.unescape(m[1]) for m in HTML_IMAGE.finditer(text)]

def _image_key(target: str) -> str:
    return unquote(target).replace('\\', '/').split('#', 1)[0]

def _check_images(body: str, folder: Path, file: str, errors: list, warnings: list, watched: dict) -> int:
    targets = _images(body)
    for target in sorted(set(targets)):
        path = _image_key(target)
        if re.match('^(?:https?://|data:)', path, re.I):
            _issue(warnings, 'remote_image_unverified', f'未联网验证图片：{target}', file=file)
            continue
        if path.startswith('//') or urlsplit(path).scheme:
            _issue(errors, 'image_path', f'图片不是论文目录中的相对文件：{target}', file=file)
            continue
        image_path = (folder / path).resolve()
        if not image_path.is_relative_to(folder.resolve()):
            _issue(errors, 'image_path', f'图片路径越出论文目录：{target}', file=file)
        elif not image_path.is_file():
            _issue(errors, 'image_missing', f'图片不存在：{target}', file=file)
        else:
            stat = image_path.stat()
            watched[image_path] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            if not stat.st_size:
                _issue(errors, 'image_empty', f'图片为空文件：{target}', file=file)
    if len(IMAGE.findall(body)) < len(re.findall('!\\[[^\\]\\n]*\\]\\(', body)):
        _issue(errors, 'image_syntax', '存在无法解析的 Markdown 图片链接。', file=file)
    if re.search('!\\[[^\\]\\n]*\\]\\[', body):
        _issue(warnings, 'image_reference_unverified', '引用式图片语法需人工核验目标。', file=file)
    return len(targets)

def _display_math(text: str, errors: list, file: str) -> list[str]:
    if len(re.findall('(?<!\\\\)\\$\\$', text)) % 2 or text.count('\\[') != text.count('\\]'):
        _issue(errors, 'formula_unbalanced', '展示公式定界符不配对。', file=file)
    return [_norm(m[0]) for m in DISPLAY.finditer(text)]

def _translation_checks(text: str, source: str, errors: list, warnings: list, file: str, unit: int) -> None:
    plain = re.sub('<!--.*?-->', '', text, flags=re.S).strip()
    if _images(plain) and image_only_paragraph(plain) and (not (image_only_paragraph(source) or label_image_paragraph(source))):
        _issue(errors, 'translation_empty', '原段含正文，译文只保留了图片。', file=file, unit=unit)
        return
    visible = re.sub('[\\s*_`#>\\-…。.]+', '', plain)
    if not visible or plain.startswith('```'):
        _issue(errors, 'translation_empty', 'quote 后缺少可见译文正文。', file=file, unit=unit)
        return
    if PLACEHOLDER.search(plain) or (len(plain) < 400 and STALL.search(plain)) or re.fullmatch('[\\s*_(（\\[]*(?:TODO|TBD|N/?A|待译|译文)[\\s*)）\\].。!！]*', plain, re.I):
        _issue(errors, 'translation_placeholder', '译文含拒绝、失败、等待或占位内容。', file=file, unit=unit)
    budget = Counter()
    for match in re.finditer(f'(?m)^[ \\t]*(?:#{{1,6}}[ \\t]*)?(\\[\\d+\\]|【\\d+】|\\d+\\)|\\d+\\.(?!\\d))', source):
        budget[match.group(1)] += 1
    for line in plain.splitlines():
        marker = MARKER.match(line)
        if not marker:
            continue
        token, _tail = marker.groups()
        if re.match('\\s*(?:#{1,6}\\s*)?\\d+\\.\\d', line):
            continue
        if budget[token] <= 0:
            _issue(errors, 'translation_marker', f'译文行首出现原段没有的内部编号：{token}', file=file, unit=unit)
        else:
            budget[token] -= 1
    prose = DISPLAY.sub('', HTML_IMAGE.sub('', WIKI_IMAGE.sub('', IMAGE.sub('', source))))
    prose = re.sub('https?://\\S+|<[^>]+>', '', prose)
    if _norm(plain) == _norm(source) and len(re.findall('[A-Za-z]+', prose)) >= 12 and (not CJK.search(plain)):
        _issue(warnings, 'translation_unchanged', '译文与原段相同，需判断是否为姓名/书目信息或漏译。', file=file, unit=unit)
    if source and len(plain) < 0.1 * len(source) and (len(source) > 200):
        _issue(warnings, 'translation_short', '译文相对原段异常短，需人工检查句内漏译。', file=file, unit=unit)

def _alignment(body: str, zh_body: str, title: str, errors: list, warnings: list, metrics: dict, file: str) -> None:
    expected, actual = (_expected_units(body), _actual_units(zh_body, title, errors, file))
    en_quotes = [_norm(u['text']) for u in expected if u['kind'] == 'quote']
    zh_quotes = [_norm(u['text']) for u in actual if u['kind'] == 'quote']
    matcher = SequenceMatcher(None, en_quotes, zh_quotes, autojunk=False)
    matched = sum((block.size for block in matcher.get_matching_blocks()))
    metrics.update(expected_quote_blocks=len(en_quotes), quote_blocks=len(zh_quotes), matched_quote_blocks=matched, quote_coverage=matched / len(en_quotes) if en_quotes else 1.0, expected_units=len(expected), actual_units=len(actual))
    if en_quotes != zh_quotes:
        _issue(errors, 'quote_coverage', 'EN→ZH 应译段落未按顺序一一覆盖（存在漏段、重复、移位或原文变动）。', file=file)
    available = Counter(en_quotes)
    for index, unit in enumerate(actual, 1):
        if unit['kind'] == 'quote':
            key = _norm(unit['text'])
            if not available[key]:
                _issue(errors, 'quote_source', 'quote 无法唯一定位到 EN 的剩余应译段落。', file=file, unit=index)
            else:
                available[key] -= 1
            _translation_checks(unit['translation'], unit['text'], errors, warnings, file, index)
        elif unit['kind'] == 'orphan':
            _issue(errors, 'translation_orphan', '出现没有对应 quote 的正文或残留内容。', file=file, unit=index)
            _translation_checks(unit['text'], '', errors, warnings, file, index)

    def key(unit):
        return (unit['kind'], '' if unit['kind'] == 'heading' else _norm(unit['text']))
    structure = SequenceMatcher(None, list(map(key, expected)), list(map(key, actual)), autojunk=False)
    differences = 0
    for op, a, b, c, d in structure.get_opcodes():
        if op == 'equal':
            for source, translated in zip(expected[a:b], actual[c:d]):
                if source['kind'] == 'heading':
                    _translation_checks(translated['text'], source['text'], errors, warnings, file, c + 1)
            continue
        differences += 1
        _issue(errors, 'structure_coverage', f'{op}: EN 单元 {a + 1}:{b} 与 ZH 单元 {c + 1}:{d} 不对应。', file=file, expected_kinds=[u['kind'] for u in expected[a:b]], actual_kinds=[u['kind'] for u in actual[c:d]])
    metrics['structure_differences'] = differences
    en_refs = [_norm(u['text']) for u in expected if u['kind'] == 'references']
    zh_refs = [_norm(u['text']) for u in actual if u['kind'] == 'references']
    if en_refs != zh_refs:
        _issue(errors, 'references_changed', '参考文献必须整块保留英文，含顺序和内容。', file=file)
    metrics['reference_blocks'] = len(en_refs)
    rendered = '\n\n'.join((u.get('translation', u['text']) for u in actual))
    en_math, zh_math = (_display_math(body, errors, 'EN'), _display_math(rendered, errors, file))
    metrics.update(display_formulas_source=len(en_math), display_formulas_translation=len(zh_math))
    if en_math != zh_math:
        _issue(errors, 'formula_mismatch', '展示公式内容、数量或顺序不同；quote 中的公式不计作译文公式。', file=file)
    en_images = Counter(map(_image_key, _images(body)))
    zh_images = Counter(map(_image_key, _images(rendered)))
    metrics.update(images_source_rendered=sum(en_images.values()), images_translation_rendered=sum(zh_images.values()))
    if en_images != zh_images:
        _issue(errors, 'image_coverage', '译文展示的图片与 EN 数量/目标不同；quote 中的图片不计入。', file=file, missing=list((en_images - zh_images).elements()), extra=list((zh_images - en_images).elements()))

def validate_paper(folder: Path, expected_key: str, expected_pdf: str, require_qc=True, require_translation=True) -> dict:
    folder = Path(folder)
    errors, warnings, metrics = ([], [], {'folder': str(folder), 'citekey': expected_key, 'input_sha256': {}})
    result = {'ok': False, 'errors': errors, 'warnings': warnings, 'metrics': metrics}
    _issue(warnings, 'semantic_review_required', SEMANTIC_LIMIT)
    _issue(warnings, 'provenance_limit', '元信息互相一致不证明 Zotero 当前记录或 PDF 科学内容正确；QC 标记不证明历史护栏实际执行。')
    if not expected_key or re.search('[<>:"/\\\\|?*\\x00-\\x1f]', expected_key) or expected_key in ('.', '..'):
        _issue(errors, 'expected_key', 'expected_key 必须是有效的单层文件名。')
        return result
    if folder.name != expected_key:
        _issue(errors, 'folder_key', '文件夹名与 expected_key 不一致。')
    pdf_name = PureWindowsPath(str(expected_pdf)).name
    if not expected_pdf or pdf_name != f'{expected_key}.pdf':
        _issue(errors, 'primary_pdf', 'expected_pdf 必须指向 <citekey>.pdf 无批注正本，不能是批注/补充/手稿等副本。')
    pdf_path = Path(expected_pdf)
    if pdf_path.is_absolute() and (not pdf_path.is_file()):
        _issue(errors, 'primary_pdf_missing', f'指定的正本文件不存在：{expected_pdf}')
    elif not pdf_path.is_absolute():
        _issue(warnings, 'pdf_existence_unverified', '仅提供 PDF 名称，未验证 Zotero 正本文件存在或其内容/批注状态。')
    if not folder.is_dir():
        _issue(errors, 'folder_missing', '论文目录不存在。')
        return result
    watched, documents = ({}, {})
    try:
        locks = sorted((p.name for p in folder.glob('.sync_*.lock')))
        metrics['locks'] = locks
        if locks:
            _issue(errors, 'active_lock', '论文目录存在同步锁；不能确认写入完成，不移除任何锁。', locks=locks)
            return result
        expected_files = {suffix: folder / f'{expected_key}_{suffix}.md' for suffix in ('EN', 'MD', 'ZH', 'Note')}
        initial_files = {p.name for p in folder.glob('*.md')}
        for path in folder.glob('*.md'):
            if re.search('_(?:EN|MD|ZH|Note)\\.md$', path.name) and path.name not in {p.name for p in expected_files.values()}:
                _issue(errors, 'filename_key', '存在其它 citekey 的派生文件；需人工处理历史文件。', file=path.name)
        for suffix, path in expected_files.items():
            if not path.is_file():
                continue
            stat = path.stat()
            watched[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            raw = path.read_bytes()
            metrics['input_sha256'][path.name] = hashlib.sha256(raw).hexdigest()
            try:
                text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
            except UnicodeError:
                _issue(errors, 'encoding', '文件不是有效 UTF-8。', file=path.name)
                continue
            fm, body, problems = _front_matter(text)
            documents[suffix] = (fm, body, path.name)
            for problem in problems:
                _issue(errors, 'front_matter', problem, file=path.name)
            for field in ('citekey', 'zotero_key', 'doi', 'title'):
                if field not in fm or (field != 'doi' and (not str(fm[field]).strip())):
                    _issue(errors, 'metadata_missing', f'缺少元信息：{field}', file=path.name)
            if fm.get('citekey') != expected_key:
                _issue(errors, 'metadata_citekey', 'YAML citekey 与 expected_key 不一致。', file=path.name)
            if not re.fullmatch('[A-Z0-9]{8}', str(fm.get('zotero_key', ''))):
                _issue(errors, 'metadata_zotero_key', 'zotero_key 应为 8 位大写字母/数字。', file=path.name)
            for linked_key in re.findall('zotero://select/library/items/([A-Za-z0-9]+)', body):
                if linked_key != fm.get('zotero_key'):
                    _issue(errors, 'metadata_zotero_link', '正文 Zotero 链接与元信息不一致。', file=path.name)
            if 'source_pdf' in fm and fm['source_pdf'] != pdf_name:
                _issue(errors, 'source_pdf', 'source_pdf 与指定正本不一致。', file=path.name)
            metrics[f'images_{suffix.lower()}'] = _check_images(body, folder, path.name, errors, warnings, watched)
            if not body.strip():
                _issue(errors, 'body_empty', '文件正文为空。', file=path.name)
        fulls = [kind for kind in ('EN', 'MD') if kind in documents]
        if len(fulls) != 1:
            _issue(errors, 'fulltext_count', '必须且只能存在一个 EN 或 MD 全文文件。')
        if 'Note' not in documents:
            _issue(errors, 'note_missing', '缺少 Note 文件；不会创建或覆盖历史笔记。')
        if fulls:
            kind = fulls[0]
            fm, body, name = documents[kind]
            if fm.get('source_pdf') != pdf_name:
                _issue(errors, 'source_pdf', '全文必须明确记录指定无批注正本 source_pdf。', file=name)
            for suffix, (other, _, other_name) in documents.items():
                for field in ('zotero_key', 'doi', 'title'):
                    if other.get(field) != fm.get(field):
                        _issue(errors, 'metadata_mismatch', f'{field} 与全文元信息不一致。', file=other_name)
                if suffix != 'Note' and (not other.get('source')):
                    _issue(errors, 'metadata_source', '缺少生成来源 source。', file=other_name)
                if suffix != kind and 'qc' in other and (other['qc'] != fm.get('qc')):
                    _issue(errors, 'qc_mismatch', '派生文件的 QC 标记与全文不同。', file=other_name)
            qc = str(fm.get('qc', '')).strip()
            metrics['qc_present'] = qc.lower() not in ('', 'false', 'none', 'null', 'no', '0', '~')
            if not metrics['qc_present']:
                _issue(errors if require_qc else warnings, 'qc_missing', '全文缺少有效 QC 标记。', file=name)
            if not require_qc:
                _issue(warnings, 'qc_requirement_disabled', '调用方显式关闭 require_qc；本结果不能作为已过 QC 的证明。')
            chinese = kind == 'MD' or str(fm.get('language', '')).lower().startswith('zh') or (bool(CJK.search(str(fm.get('title', '')))) and (not KANA.search(str(fm.get('title', ''))))) or (len(CJK.findall(body)) > 0.25 * max(1, len(CJK.findall(body)) + len(re.findall('[A-Za-z]', body))) and len(KANA.findall(body)) < 5)
            metrics.update(fulltext_kind=kind, chinese_source=bool(chinese), translation_required=bool(require_translation and (not chinese)))
            if not require_translation:
                _issue(warnings, 'translation_requirement_disabled', '调用方允许缺少翻译；已有 ZH 仍接受完整检查。')
            if 'ZH' in documents:
                if chinese:
                    _issue(warnings, 'chinese_has_translation', '中文原文无需 ZH；已有历史 ZH 仅检查，不会覆盖或删除。')
                _alignment(body, documents['ZH'][1], str(fm.get('title', '')), errors, warnings, metrics, documents['ZH'][2])
            elif require_translation and (not chinese):
                _issue(errors, 'translation_missing', '外文论文缺少 ZH 翻译。')
            else:
                metrics['display_formulas_source'] = len(_display_math(body, errors, name))
            if not fm.get('doi'):
                _issue(warnings, 'doi_empty', '元信息未提供 DOI；允许无 DOI 文献，未联网验证。')
        if initial_files != {p.name for p in folder.glob('*.md')}:
            _issue(errors, 'input_changed', '检查过程中 Markdown 文件集合改变。')
        for path, old in watched.items():
            if not path.is_file():
                _issue(errors, 'input_changed', '检查过程中文件消失。', file=path.name)
                continue
            stat = path.stat()
            if old != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                _issue(errors, 'input_changed', '检查过程中文件发生变化，应在 worker 完成后重试。', file=path.name)
        locks = sorted((p.name for p in folder.glob('.sync_*.lock')))
        if locks:
            _issue(errors, 'active_lock', '检查结束时出现同步锁，应重新检查。', locks=locks)
    except (OSError, ValueError) as exc:
        _issue(errors, 'read_error', f'无法完成只读检查：{exc}')
    result['ok'] = not errors
    return result

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, default=LIBRARY, help='Library root (read-only)')
    parser.add_argument('--only', '--key', action='append', default=[], help='citekey(s), comma-separated; repeatable')
    parser.add_argument('--expected-pdf', help='single-paper clean-primary basename or absolute path')
    parser.add_argument('--limit', type=int, default=0, help='maximum papers; 0 = all selected')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='default: stdout only, no writes')
    mode.add_argument('--apply', action='store_true', help='write only a NEW reports/library_qa_*.json')
    parser.add_argument('--no-require-qc', action='store_true', help='explicit QC exception, recorded in warnings')
    parser.add_argument('--no-require-translation', action='store_true', help='permit absent ZH, still validate existing ZH')
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit must be >= 0')
    papers = args.library / 'papers'
    if not papers.is_dir():
        parser.error(f'papers directory not found: {papers}')
    keys = list(dict.fromkeys((k.strip() for group in args.only for k in group.split(',') if k.strip())))
    if args.only and (not keys):
        parser.error('--only must not be empty')
    if not args.only:
        keys = sorted((p.name for p in papers.iterdir() if p.is_dir()))
    if args.limit:
        keys = keys[:args.limit]
    if args.expected_pdf and len(keys) != 1:
        parser.error('--expected-pdf requires exactly one selected paper')
    results = [validate_paper(papers / key, key, args.expected_pdf or f'{key}.pdf', not args.no_require_qc, not args.no_require_translation) for key in keys]
    report = {'schema_version': 1, 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'mode': 'apply' if args.apply else 'dry-run', 'ok': bool(results) and all((r['ok'] for r in results)), 'papers_checked': len(results), 'results': results}
    if args.apply:
        try:
            root = DATA.resolve() if DATA != REPO else Path(__file__).resolve().parents[1]
            if not REPORTS.resolve().is_relative_to(root) or REPORTS.resolve().is_relative_to(args.library.resolve()):
                raise ValueError('reports must remain inside the configured data root and outside Library')
            REPORTS.mkdir(parents=True, exist_ok=True)
            destination = REPORTS / f'library_qa_{dt.datetime.now():%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex[:8]}.json'
            report['report_path'] = str(destination)
            with destination.open('x', encoding='utf-8') as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
        except (OSError, ValueError) as exc:
            print(f'report write failed: {exc}', file=sys.stderr)
            return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1
if __name__ == '__main__':
    raise SystemExit(main())
