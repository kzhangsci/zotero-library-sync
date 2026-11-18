# 首次使用前：Better BibTeX v2 键与期刊映射

此 beta 要求先配置 Better BibTeX 的 citation key 公式与自己的期刊缩写表，再对小批
条目 dry-run。默认 BBT 键可能不满足格式校验；不承诺开箱处理任意论文。
安装器不会自动修改 BBT 设置、刷新全库键、改名附件或补跑旧条目。

在 Zotero 的 Better BibTeX 设置中，把 Citation key formula 设置为以下完整四分支公式
（分号分隔，首个适用分支生效）：

```text
type('bookSection','book','thesis','report','presentation','dataset','webpage','document','standard','letter','artwork','bill') + language('zh') + auth.transliterate.capitalize + year + veryshorttitle(2,2).prefix('-') + 'CN'; type('bookSection','book','thesis','report','presentation','dataset','webpage','document','standard','letter','artwork','bill') + auth.nopunct.capitalize + year + veryshorttitle(2,2).prefix('-'); language('zh') + auth.transliterate.capitalize + year + (extra('tex.jabbr') || journal('abbrev')) + veryshorttitle(2,2).prefix('-') + 'CN'; auth.nopunct.capitalize + year + (extra('tex.jabbr') || journal('abbrev')) + veryshorttitle(2,2).prefix('-')
```

规则是作者 + 四位年份 + 可选期刊段 + 连字符 + 短标题；中文分支转写作者并加 `CN`。
书籍、学位论文、报告等列出的类型不带期刊段；会议缩写由 intake 使用 `tex.jabbr: MEETING`。
处理中文需要相应 BBT 中文分词/转写支持；更改公式后先在合成或明确选中的测试条目上核对
键结果。已有固定/pinned 键未必随公式改变，不要对整个私人库执行批量刷新。

后台接受的键格式为：

```regex
^[A-Za-z][A-Za-z0-9-]*\d{4}[A-Za-z0-9]*-[A-Za-z0-9]+(CN)?[a-z]?$
```

例如合成条目 `Example2099SYN-Fixture`。这是格式示例，不是任何真实文献或 item key。

期刊缩写分两层。随包公共基础表 `app/release/journal_acronyms.base.csv` 收录常见
海洋/地学期刊（JPO、GRL、JGR、DSR、OS、OM、SciRep、RS、SciData、NC、GMD 等及常见
拼写变体）到 `title,proposed` 短码的映射；这些短码是**文件名/引用键命名约定，不是
ISO 4 缩写声明**，来源于公开刊名的整理。个人覆盖表 `data/reports/journal_acronyms.csv`
初始只有表头：基础表先生效，**个人覆盖优先**（同一刊名以你的行为准），更新版本不会
批量改名或改写你的映射；覆盖表也兼容带额外列的历史草稿格式（含 `title` 与 `proposed`
列即可）。同表内同一刊名映射到两个不同短码会显式报错；以 `=`、`+`、`-`、`@` 开头或含
控制字符的刊名按公式注入拒绝，短码仅限 1–12 位字母数字。

预演时未映射的 journalArticle 仍会被明确阻塞并列出刊名；可在面板“期刊缩写快捷补表”
按刊保存（逗号/引号/Unicode 会正确转义），保存后重新预演。合成示例仅供参考
（`journal_acronyms.example.csv`），安装器不会把合成行混入任何一张表。
先配置 BBT v2 与缩写 → 在 Zotero 明确选择一篇 → 设置上限 1 → 预演确认键与附件 →
解决阻碍后再执行同步。已有作者命名、期刊规范或 pinned 键必须由用户自行决定兼容方式。
