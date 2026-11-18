# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.
from __future__ import annotations
import difflib
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
MIN_LEN = 20
LEN_TOL = 0.15
SIM_MIN = 0.8
ABS_LOSS = 60
SHORT_LEN = 40
TAGS = re.compile('<[^>]+>')
SENT_END = re.compile('[.!?。！？](?=\\s|$)')
BLOCKS = re.compile('!\\[|\\$\\$')
CHUNK_CHARS = int(os.environ.get('QC_CHUNK_CHARS') or 12000)
MIN_REJECTS = 3
QC_PARALLEL = int(os.environ.get('QC_PARALLEL') or 1)
PROMPT = '【第一原则】这**不是翻译任务**。输出必须和输入是同一种语言：英文段落改完仍然是英文。\n任何把英文改成中文的回答都会被直接丢弃，不要那样做。\n\n你是学术文献的排版校对，处理的是 MinerU 从 PDF 解析出来的 Markdown。\n只修复「解析造成的错误」，绝对不要改动作者原意、不要翻译、不要润色语言、不要增删句子、不要改写表达。\n原文本身的笔误（拼写错误、公式里明显写错的符号或下标，例如把 Taylor–Goldstein 写成 Golstein、把 ρ₂−ρ₁ 写成 ρ₂−ρ₂）也**保持原样**，不要订正：全文要忠于 PDF 原文，你只修 MinerU 解析造成的错误。\n\n已知的解析缺陷类型：\n1. 符号字体被解析成错字：控制字符或 `$` 出现在本该是 ± × ° ≥ ≤ ≈ ∼ · − 的位置。\n   例：`T $ 128C` 应为 `T ≥ 12°C`；`jz/fj $ 1` 应为 `|ζ/f| ≥ 1`；`values $ 20%` 应为 `values ≥ 20%`。\n   注意：真正的美元金额（`$400M`、`$1.5 million`）不是错误，保持原样。\n   希腊字母也常被符号字体变成拉丁字母（Symbol 字体对照：a=α b=β g=γ d=δ e=ε z=ζ h=η q=θ k=κ l=λ m=μ n=ν x=ξ p=π r=ρ s=σ t=τ u=υ f=φ c=χ y=ψ w=ω；D=Δ F=Φ G=Γ L=Λ P=Π Q=Θ S=Σ W=Ω X=Ξ Y=Ψ）。\n   这张对照表只是常见例子：不同期刊用的字体不同（如 AMS 期刊 JPO、JAS 里是 u=θ、s=σ，“u1–S” 就是 θ₁–S），一律以论文自己的公式里用的字母为准，不要照表硬套。\n   只有论文自己的公式里用的正是对应的希腊字母时才替换，而且同一处要换全：例如公式里是 η(x,t)[φ(z)+…]，正文的 h f (z) 就要同时换成 η 和 φ（下标等以论文公式为准），不能只换 h 不换 f。\n   这条只针对被变成拉丁字母的字；原文里本来就是数学符号的（如 ∅、∂、∇、∑，或已经写成 varnothing、emptyset 这类命令的），即使看起来像作者写错了也**保持原样**，不要按字形或语义去猜着改。\n2. 连字丢失（fi/fl/ffi 被吞）：`su$cient`→`sufficient`、`O$ce`→`Office`、`signicant`→`significant`、\n   `ow`→`flow`、`eld`→`field`、`rst`→`first`、`dierent`→`different`。\n3. LaTeX 排版错误：`$…$` 不配对、\n   花括号缺失、上下标错位。\n   跨行的 `$…$` 公式块本身是正常的，不要动。\n   公式与标点、括号之间的多余空格已经统一处理过；公式**内部**的空格（如 `\\mathrm { k g }`、`N = 14`）渲染时不显示，**不要**为它改动段落。\n   单位写在公式内还是公式外都算正确，**不要**为了把单位挪进公式、给单位加 mathrm 或细空格而改动段落；只修真正写错的单位，例如被写成 ms⁻¹（毫秒⁻¹）的米每秒应为 m s⁻¹。\n4. 逐词重复的 <sub>/<sup> 噪声：`Geo<sub>p</sub>h<sub>y</sub>s.` 应为 `Geophys.`。\n5. 单词被拆开或粘连：`th e`、`thehypothesis`、`efectiveness`→`effectiveness`；行内多余空格。\n6. Markdown 标题井号丢失、表格结构塌掉。\n\n下面是编号的段落。**只输出你要修改的段落**，格式严格为：\n\n[n]\n修改后的完整段落\n\n一个都不需要改就只输出一个字：无\n不要输出解释，不要输出没有改动的段落，不要合并或拆分段落。\n再次强调：**保持原文语言不变**，只动排版和符号；段落大多是不需要改的，宁可不改也不要改错。\n\n段落：\n'
LIGATURE = re.compile('\\b(?:sucient|suciently|ecient|eciency|eective|eectively|eect|eects|dierent|dierence|dierences|dicult|diculty|coecient|coecients|oce|specic|specically|signicant|signicantly|signicance|classication|identied|dened|denition|denitions|conguration|congurations|prole|proles|proled|uxes|uid|uids|owing|nite|innite|conrm|conrmed|reect|reects|reected|sufce|sufcient)\\b')
SUSPECT = re.compile('[\\x01-\\x05]|<su[bp]>|\\$|\\\\[a-zA-Z]|[A-Za-z]{2,}\\s[a-z]\\s[a-z]{2,}')
IMG = re.compile('^\\s*!\\[')
TABLE = re.compile('^\\s*<table', re.I)
DISPLAY = re.compile('^\\s*\\$\\$|^\\s*\\$\\s*$')
NUM = re.compile('(?m)^\\s*(?:\\[(\\d+)\\]|【(\\d+)】)\\s*$')
QC_MARK = re.compile('(?m)^[^\\S\\r\\n]*(?:\\[[^\\]\\r\\n]*\\]|【[^】\\r\\n]*】)[^\\S\\r\\n]*\\r?$')
QC_REFUSAL = re.compile("^(?:(?:sorry|i(?:'m| am) sorry)[,.:!\\s]*)?i\\s+(?:cannot|can['’]t|am unable to)\\s+(?:(?:process|complete|fulfill?|perform)\\s+(?:this|your)\\s+(?:request|task)|(?:comply|assist|help) with (?:this|your) request)\\b|^(?:抱歉|对不起)[，,:：。\\s]*(?:我)?(?:无法|不能)|^(?:我)?(?:无法|不能)(?:处理|完成|执行|满足)(?:此|该|这[个项]?|你的|您的)(?:请求|任务)", re.I)

class QCProtocolError(RuntimeError):
    stop_workflow = True

    def __init__(self, reason: str):
        super().__init__(f'invalid QC response: {reason}')

def skip(p: str) -> bool:
    s = p.strip()
    if len(s) < MIN_LEN or IMG.match(s) or TABLE.match(s) or DISPLAY.match(s):
        return True
    return not (SUSPECT.search(s) or LIGATURE.search(s))

def parse_changes(text: str, n: int) -> dict[int, str]:
    if not isinstance(text, str) or not text.strip():
        raise QCProtocolError('empty or non-text answer')
    if text.strip() in ('无', '无。'):
        return {}
    out: dict[int, str] = {}
    marks = list(QC_MARK.finditer(text))
    if not marks:
        raise QCProtocolError('expected numbered replacements or explicit 无 / 无。')
    if text[:marks[0].start()].strip():
        raise QCProtocolError('unparsed prefix before first paragraph ID')
    for i, m in enumerate(marks):
        valid = NUM.fullmatch(m.group())
        if not valid:
            raise QCProtocolError('malformed paragraph ID')
        try:
            num = int(valid.group(1) or valid.group(2))
        except ValueError:
            raise QCProtocolError('malformed paragraph ID') from None
        if not 1 <= num <= n:
            raise QCProtocolError('paragraph ID outside input range')
        if num in out:
            raise QCProtocolError('duplicate paragraph ID')
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if not body:
            raise QCProtocolError('empty replacement')
        if body in ('无', '无。', 'None') or QC_REFUSAL.match(body):
            raise QCProtocolError('no-change marker or refusal used as replacement')
        out[num] = body
    return out
UNI_SPACE = re.compile('[\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]')
CJK_CHARS = re.compile('[' + chr(19968) + '-' + chr(40959) + ']')

def _plain(s: str) -> str:
    return re.sub('\\s+', ' ', TAGS.sub('', s)).strip()

def _skeleton(s: str) -> str:
    s = unicodedata.normalize('NFKD', TAGS.sub('', s))
    return ''.join((c for c in s if c.isalnum())).casefold()

def judge(old: str, new: str) -> str:
    if new == old:
        return 'identical'
    if UNI_SPACE.sub(' ', old) == UNI_SPACE.sub(' ', new):
        return 'whitespace class only'
    if not CJK_CHARS.search(old) and CJK_CHARS.search(new):
        return 'adds Chinese characters'
    sk = _skeleton(old)
    if sk and len(_plain(old)) <= SHORT_LEN and (sk == _skeleton(new)):
        return ''
    if abs(len(new) - len(old)) > LEN_TOL * max(1, len(old)):
        return f'length {len(old)}->{len(new)}'
    po, pn = (_plain(old), _plain(new))
    loss = len(po) - len(pn)
    if loss > ABS_LOSS:
        return f'deletes {loss} characters'
    n_old, n_new = (len(SENT_END.findall(po)), len(SENT_END.findall(pn)))
    if loss > 20 and n_new < n_old:
        return f'drops a sentence ({n_old}->{n_new}, {loss} characters)'
    if len(BLOCKS.findall(new)) < len(BLOCKS.findall(old)):
        return 'drops an image or display formula'
    sim = difflib.SequenceMatcher(None, old, new).ratio()
    if sim < SIM_MIN:
        return f'similarity {sim:.2f}'
    return ''

def diff_words(old: str, new: str, limit: int=3) -> str:
    sm = difflib.SequenceMatcher(None, old, new)
    bits = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        a, b = (old[max(0, i1 - 12):i2 + 12].strip(), new[max(0, j1 - 12):j2 + 12].strip())
        bits.append(f'{a!r} -> {b!r}')
        if len(bits) >= limit:
            break
    return '; '.join(bits)
TYPO_QUOTES = {'’': "'", '‘': "'", '“': '"', '”': '"'}
QUOTES_ASCII = str.maketrans(TYPO_QUOTES)
MD_PROTECTED = re.compile('\\$\\$.*?\\$\\$|\\$[^$]*\\$|<[^>]*>|`[^`]*`|\\]\\([^)]*\\)', re.S)
QUOTE_TOKEN = re.compile('[一-鿿]\\s*|[A-Za-z0-9]+\\s*|[^\\sA-Za-z0-9一-鿿]\\s*|\\s+')

def keep_quote_style(old: str, new: str) -> str:
    if not any((q in old for q in TYPO_QUOTES)):
        return new
    ta = list(QUOTE_TOKEN.finditer(old.translate(QUOTES_ASCII)))
    tb = list(QUOTE_TOKEN.finditer(new.translate(QUOTES_ASCII)))
    sm = difflib.SequenceMatcher(None, [m.group() for m in ta], [m.group() for m in tb], autojunk=False)
    protected = set()
    for m in MD_PROTECTED.finditer(new):
        protected.update(range(m.start(), m.end()))
    out = list(new)
    for ai, bj, size in sm.get_matching_blocks():
        for k in range(size):
            sa, sb = (ta[ai + k].start(), tb[bj + k].start())
            for off, ch in enumerate(old[sa:ta[ai + k].end()]):
                if ch in TYPO_QUOTES and new[sb + off] == TYPO_QUOTES[ch] and (sb + off not in protected):
                    out[sb + off] = ch
    return ''.join(out)

def qc_chunk(paras: list[str], be, cache_dir=None) -> tuple[dict[int, str], list[str]]:
    if not paras:
        return ({}, [])
    numbered = '\n\n'.join((f'[{i + 1}]\n{p}' for i, p in enumerate(paras)))
    try:
        if cache_dir is None:
            text = be.ask(PROMPT + numbered)
        else:
            from glm_concurrency import qc_reply
            text = qc_reply(PROMPT + numbered, be, cache_dir, lambda reply: parse_changes(reply, len(paras)))
    except Exception as e:
        if getattr(e, 'stop_workflow', False):
            raise
        return ({}, [f'backend error: {str(e)[:120]}'])
    ok, rejected = ({}, [])
    for i, new in parse_changes(text, len(paras)).items():
        new = keep_quote_style(paras[i - 1], new)
        why = judge(paras[i - 1], new)
        if why == 'identical':
            continue
        if why:
            rejected.append(f'rejected [{i}] ({why}): {diff_words(paras[i - 1], new, 1)}')
        else:
            ok[i] = new
    if len(rejected) >= MIN_REJECTS and len(rejected) > len(ok):
        return ({}, [f'chunk dropped: {len(rejected)} of {len(rejected) + len(ok)} proposals failed the guardrails'] + rejected)
    return (ok, rejected)

def rejection_count(notes: list[str]) -> int:
    return sum((note.startswith('rejected [') for note in notes))

def chunks(paras: list[str], max_chars: int=CHUNK_CHARS):
    cur, n = ([], 0)
    for p in paras:
        if cur and n + len(p) > max_chars:
            yield cur
            cur, n = ([], 0)
        cur.append(p)
        n += len(p)
    if cur:
        yield cur
TOKEN = re.compile('[一-鿿]|[^\\s一-鿿]+')
TAG = re.compile('<[^>]+>|!\\[[^\\]]*\\]\\([^)]*\\)')
CMD = re.compile(re.escape(chr(92)) + '([A-Za-z]+)')
GREEK = {'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ', 'epsilon': 'ε', 'varepsilon': 'ε', 'zeta': 'ζ', 'eta': 'η', 'theta': 'θ', 'vartheta': 'θ', 'iota': 'ι', 'kappa': 'κ', 'lambda': 'λ', 'mu': 'μ', 'nu': 'ν', 'xi': 'ξ', 'pi': 'π', 'rho': 'ρ', 'varrho': 'ρ', 'sigma': 'σ', 'tau': 'τ', 'upsilon': 'υ', 'phi': 'φ', 'varphi': 'φ', 'chi': 'χ', 'psi': 'ψ', 'omega': 'ω', 'Gamma': 'Γ', 'Delta': 'Δ', 'Theta': 'Θ', 'Lambda': 'Λ', 'Xi': 'Ξ', 'Pi': 'Π', 'Sigma': 'Σ', 'Upsilon': 'Υ', 'Phi': 'Φ', 'Psi': 'Ψ', 'Omega': 'Ω', 'circ': '°', 'pm': '±', 'times': '×', 'sim': '∼', 'approx': '≈', 'leq': '≤', 'le': '≤', 'geq': '≥', 'ge': '≥', 'prime': '′'}
FUNC_NAMES = {'log', 'ln', 'exp', 'sin', 'cos', 'tan', 'cot', 'sinh', 'cosh', 'tanh', 'arcsin', 'arccos', 'arctan', 'max', 'min', 'sup', 'inf', 'lim', 'det', 'arg', 'deg', 'dim', 'exp', 'mod'}
SYMBOLIC = re.compile('[α-ωΑ-Ω°±×∼≈≤≥‰−=<>′″|]')
MATH_MINUS = re.compile('\\^\\s*\\{?\\s*-|\\$\\s*-|\\{\\s*-')
NONWORD = re.compile('[\\W_]+')
GLYPH_LIGATURE = re.compile('(?<=[A-Za-z])[VWXY]|[VWXY](?=[a-z])|[' + chr(142) + chr(143) + ']')
GLYPH_LIGATURE_AFTER = re.compile('f[fil]')
GLYPH_QUOTES = re.compile('[“”‘’"]')
CONTEXT = 30
PDF_MIN_CHARS = 3000
GUARD_MIN_CHARS = 2

def clean(s: str) -> str:
    return CMD.sub(lambda m: GREEK.get(m.group(1)) or (m.group(1) if m.group(1) in FUNC_NAMES else ' '), TAG.sub(' ', s))

def norm(s: str) -> str:
    s = ''.join((ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch))).replace('ı', 'i')
    return NONWORD.sub('', unicodedata.normalize('NFKC', s).lower())

def pdf_norm(pdf) -> str:
    import fitz
    fitz.TOOLS.mupdf_display_errors(False)
    with fitz.open(str(pdf)) as doc:
        text = norm(''.join((page.get_text() for page in doc)))
    return text if len(text) >= PDF_MIN_CHARS else ''

def judge_span(a_tok: list[str], b_tok: list[str], x1: int, x2: int, y1: int, y2: int, P: str) -> tuple[str, int]:
    before, after = (clean(' '.join(a_tok[x1:x2])), clean(' '.join(b_tok[y1:y2])))
    nb, na = (norm(before), norm(after))
    if nb == na:
        return ('format', 0)
    size = sum((max(i2 - i1, j2 - j1) for t, i1, i2, j1, j2 in difflib.SequenceMatcher(None, nb, na, autojunk=False).get_opcodes() if t != 'equal'))
    if GLYPH_LIGATURE.search(before) and GLYPH_LIGATURE_AFTER.search(after) and (not GLYPH_LIGATURE_AFTER.search(before)):
        return ('glyph', size)
    if GLYPH_QUOTES.search(after) and (not GLYPH_QUOTES.search(before)) and (na in nb):
        return ('glyph', size)
    gains_symbol = SYMBOLIC.search(unicodedata.normalize('NFKC', after)) and (not SYMBOLIC.search(unicodedata.normalize('NFKC', before)))
    gains_minus = MATH_MINUS.search(after) and (not MATH_MINUS.search(before))
    if gains_symbol or gains_minus:
        return ('font', size)
    lp, rp = (norm(clean(' '.join(a_tok[max(0, x1 - 40):x1])))[-CONTEXT:], norm(clean(' '.join(a_tok[x2:x2 + 40])))[:CONTEXT])
    lc, rc = (norm(clean(' '.join(b_tok[max(0, y1 - 40):y1])))[-CONTEXT:], norm(clean(' '.join(b_tok[y2:y2 + 40])))[:CONTEXT])
    if len(lp) + len(rp) < 40:
        return ('unknown', size)
    in_b, in_a = (lp + nb + rp in P, lc + na + rc in P)
    if in_b and (not in_a):
        return ('source-edit', size)
    if in_a and (not in_b):
        return ('parse-fix', size)
    return ('unknown', size)

def pdf_check(old: str, new: str, P: str, left: str='', right: str='') -> list[tuple[int, str, str]]:
    a_tok, b_tok = (TOKEN.findall(f'{left}\n{old}\n{right}'), TOKEN.findall(f'{left}\n{new}\n{right}'))
    out = []
    for tag, x1, x2, y1, y2 in difflib.SequenceMatcher(None, a_tok, b_tok, autojunk=False).get_opcodes():
        if tag != 'equal':
            verdict, size = judge_span(a_tok, b_tok, x1, x2, y1, y2, P)
            if verdict == 'source-edit':
                out.append((size, ' '.join(a_tok[x1:x2]), ' '.join(b_tok[y1:y2])))
    return out
UNANSWERED_ROUNDS = 2
UNANSWERED_WAIT = 300

def qc_body(body: str, be, pdf_text: str='', chunk_cache=None) -> tuple[str, list[str], list[str]]:
    todo = [p.strip() for p in re.split('\\n\\s*\\n', body) if p.strip() and (not skip(p))]
    out_body = body
    accepted, notes = ([], [])

    def use(group: list[str], ok: dict[int, str]) -> None:
        nonlocal out_body
        for i, new in ok.items():
            old = group[i - 1]
            pos = out_body.find(old)
            if pos < 0:
                continue
            if pdf_text:
                edits = [e for e in pdf_check(old, new, pdf_text, out_body[max(0, pos - 300):pos], out_body[pos + len(old):pos + len(old) + 300]) if e[0] >= GUARD_MIN_CHARS]
                if edits:
                    size, before, after = edits[0]
                    notes.append(f'rejected [pdf] ({len(edits)} span(s) away from the PDF text layer, e.g. {before[:60]!r} -> {after[:60]!r}, {size} chars)')
                    continue
            out_body = out_body[:pos] + new + out_body[pos + len(old):]
            accepted.append(diff_words(old, new))

    def run(groups: list[list[str]]) -> list[tuple[list[str], dict[int, str], list[str]]]:

        def ask(group):
            return qc_chunk(group, be, chunk_cache) if chunk_cache is not None else qc_chunk(group, be)
        if QC_PARALLEL <= 1 or len(groups) < 2:
            return [(g, *ask(g)) for g in groups]
        with ThreadPoolExecutor(max_workers=min(QC_PARALLEL, len(groups))) as pool:
            return [(g, *r) for g, r in zip(groups, pool.map(ask, groups))]
    budget = CHUNK_CHARS
    profile = getattr(be, 'cache_identity', {})
    if profile.get('provider') == 'tongji' and profile.get('model') == 'DeepSeek-V4.1-Flash' and (profile.get('reasoning_effort') == 'max'):
        budget = min(budget, 3500)
    pending = []
    for group, ok, n in run(list(chunks(todo, budget))):
        if any((x.startswith('backend error') for x in n)):
            pending.append((group, n))
            continue
        notes += n
        use(group, ok)
    for round_no in range(1, UNANSWERED_ROUNDS + 1):
        if not pending:
            break
        print(f'   qc: {len(pending)} chunk(s) got no answer; asking again in {UNANSWERED_WAIT}s (round {round_no}/{UNANSWERED_ROUNDS})', flush=True)
        time.sleep(UNANSWERED_WAIT)
        still = []
        for group, ok, n in run([g for g, _ in pending]):
            if any((x.startswith('backend error') for x in n)):
                still.append((group, n))
            else:
                notes += n
                use(group, ok)
        pending = still
    for _, n in pending:
        notes += n
    return (out_body, accepted, notes)
