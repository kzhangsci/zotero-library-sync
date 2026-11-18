# Library Sync · Zotero 文献同步

支持 Windows x64，并新增 macOS 13+（Apple Silicon / Intel）候选支持的 Zotero 10.0 插件。macOS 安装方式和实际验收范围见 [macOS 指南](release/MACOS.md)。选中文献或检测任意分类中的新增条目，预演后在后台整理正本和批注本、解析 PDF、质检，并按需生成段落对照的中文译文。任务页显示验收、索引与本地 Git 提交进度。

**beta.16 更新**：保留 PyMuPDF 和 MinerU；新增 macOS 路径/启动、Keychain 凭据、进程身份、任务锁、内容快照提交和离线安装入口。自有代码采用 MIT，包含 PyMuPDF/MuPDF 的组合发行遵守 AGPL，见 [发行许可说明](release/DISTRIBUTION_LICENSE.md)。macOS 功能以实际验收范围为准。

**beta.15 更新**：混合失败的批次也能安全暂停——已有验收、已暂停与未通过验收的文献在同一张暂停凭据里各自留证，恢复只补健康子集的缺失步骤，失败文献进入“待处理清单”而不阻塞、也不被伪装成已通过；历史遗留的“请求过暂停但未生成凭据”受阻任务提供证据核验式恢复（只读核验后启动新子任务，原记录不改动）。恢复前可预览“原任务冻结模型 vs 当前已保存模型”并显式选择，默认沿用冻结配置，选择后若保存的配置再变化则拒绝执行。任务页每篇文献新增“详情”：错误码中文释义、失败阶段、文件/单元证据、日志尾部、问题台账状态与可用处置。处置动作先预览再确认（一次性令牌绑定文献字节、模型配置与身份，变化须重新预览），模型类动作全部在隔离候选目录执行并完整验收，通过才逐文件发布并可回滚；“仅重新翻译中文”从核验过的当前 EN 生成全新 ZH 候选，不重解析 PDF。新增随包公共期刊缩写基础表（常见海洋/地学期刊的文件名/引用键短码，非 ISO 4 缩写），个人覆盖表优先；未知期刊在预演中可直接补表保存。三个月报告清理不会删除活跃恢复证据、未决问题台账或排除偏好。

**beta.9 更新**：检测结果、逐篇包含/翻译勾选与模型设置三者分离——预演后修改模型只标记“绑定待更新”，保存配置后经 `/v1/preview/rebind` 本地更新预演模型快照（不联网、不重新检测），检测范围、附件计划、待办与逐篇选择全部保留，签发新预演编号并作废旧编号。任务运行中可把模型修改保存为“下一任务”草稿（Key 与 MinerU 更新仍需任务结束）。模型目录独立成通道：QC/ZH 并行加载、同身份请求合并、10 分钟缓存并按凭据代际失效。预演摘要按当前逐篇选择把可执行条目分为“预计需补齐”与“已有成果待复核”。

## 下载和安装

从 [GitHub Releases](https://github.com/kzhangsci/zotero-library-sync/releases) 下载对应版本 XPI（本地 beta.16 候选产物位于 release/dist），在 Zotero 的插件管理器中从文件安装。新安装还需要 Windows 后台包。已有用户应等当前任务结束后更新插件，并重启空闲后台服务以加载后台修复。以下命令可用源码和官方依赖生成完整安装包。准备 Windows x64、Python 3.12 或以上、Git、Zotero 和 Better BibTeX，在下载并解压的源码根目录运行：

```powershell
py -3 -B release/fetch_dependencies.py --apply
py -3 -B tools/build_library_release.py --apply
```

生成的 Windows ZIP 位于 `release/dist`。第三方运行时由构建者从上游下载，构建器逐项校验锁文件中的 SHA-256。GitHub Release 提供插件、源码和 Windows 后台包。自有源码采用 MIT；集成 PyMuPDF/MuPDF 的组合发行遵守 AGPL。第三方许可与对应源码义务见发行许可说明。

## 配置 Windows 后台

解压 `library-sync-0.2.0-beta.15-windows-x64.zip` 到普通目录。先预演：

```powershell
.\setup.ps1 -LibraryDir 'D:\Research\Library' -BaselineDate '2026-10-25' `
  -InitLibraryGit -GitUserName 'Your Name' -GitUserEmail 'you@example.org'
```

确认日期和目录后，使用相同参数加 `-Apply`：

```powershell
.\setup.ps1 -LibraryDir 'D:\Research\Library' -BaselineDate '2026-10-25' `
  -InitLibraryGit -GitUserName 'Your Name' -GitUserEmail 'you@example.org' -Apply
```

默认安装目录是 `%LOCALAPPDATA%\LibrarySync`；可用 `-InstallDir` 选择其他目录。
安装脚本不要求管理员权限，不创建服务或计划任务，不启动同步。
先从 [Git for Windows 官方站](https://gitforwindows.org/) 安装 Git，或提供
`-GitPath 'C:\Program Files\Git\cmd\git.exe'`。setup 只检查并使用它，不自动下载或全局安装。
Library 尚非仓库时必须显式 `-InitLibraryGit` 并填写自己的 Git 姓名/邮箱；仅写该库本地
Git 配置，不暂存/提交已有内容，不设 remote。已是仓库时省略 `-InitLibraryGit`，保留原配置。
每次新安装的 DATA 均初始化独立私有 Git 用于报告历史，因此姓名/邮箱参数仍必填；
DATA 的 `.gitignore` 排除凭据、缓存、解析产物、output 和 CLI 工作目录。
它保留已有 Library 内容，并拒绝覆盖已有 app/data 安装；升级前应保留旧包和数据。
`-Limit 0` 或 `-Limit 1` 均表示一个完整安装，不代表文献处理上限。

目录：`app` 放代码和运行时；`data` 放报告、缓存及后续由用户保存的 DPAPI 凭据；
Library 由用户单独选择。安装配置为 `app/library-sync.json`，没有个人预设路径或密钥。
默认 Zotero 数据目录为当前用户的 `Zotero`，自定义数据目录可在配置中填写 `zotero_data_dir`。

在 Zotero 的插件管理界面从文件安装 `app/addon/library-sync-0.2.0-beta.15.xpi`。仅安装 XPI 不包含本版 Python 后台变更（暂停/恢复增强、诊断与处置、期刊基础表都在后台）：面板会按能力握手检测旧后台并禁用相应入口、给出升级提示，不影响正在运行的任务；请同时更新 Windows 后台包。升级时保留原有 `data` 目录即可保留个人期刊覆盖表与问题台账。
默认安装可自动定位；自定义安装需在插件设置中填写 `app` 与 `app/runtime/pythonw.exe`。
已保存的旧插件路径优先于自动发现，迁移时应明确更新这两个路径。
更新源为保留域名 `.invalid`；当前只支持手动更新，没有部署自动更新服务。

## 首次 baseline 与日常使用

开始前必须完成 [BBT v2 公式与期刊映射设置](release/BBT_SETUP.md)。默认 BBT 键和空缩写表
可能直接阻塞所有期刊条目；该 beta 不承诺开箱处理任意论文。

`BaselineDate` 必填，不自动用今天、不回推到最早条目。安装写入空的新增发现台账，
边界为指定日期的 **UTC 00:00**；正常新增发现只从此边界开始，避免旧库自动补跑。
选择旧日期仍可能纳入很多条目，务必先预演并设置小的文献上限。
明确选择旧条目是另一项用户操作，不应与自动新增发现混淆。

首次在插件设置中授权 Zotero 本地 API，分别配置解析/模型服务，再预演并确认同步。

原文质检、中文翻译可各自选择服务商。新版给新配置预填 GLM Coding Plan 地址。已有设置继续使用保存的地址与模型。先测试对应角色，再保存需要的配置。如果上次强制停止了同步，可以重新预演原条目，或从“检测新增”中选回待处理文献。
安装与离线 smoke 没有验证这些真实服务，也没有运行真实模型。
后台启动后及持续运行期间每天清理一次超过三个自然月的运行报告。清理范围为 `reports` 中程序生成的 `sync_日期_时间.md` 和 `library_sync_日期_时间_编号.md`，运行中、暂停任务和近期修改的报告会保留。任务状态、恢复收据、论文文件及专题报告继续用于查询与恢复。
支持暂停、继续未完成步骤、仅重试收尾与遗留暂停恢复；CLI 入口提供 `--pause`、`--resume-run`
（含 `--recover-legacy` 证据核验恢复）。继续/恢复默认沿用原任务冻结模型；面板会先展示
冻结旧配置与当前已保存配置的差异，经确认后才改用已保存配置。失败文献的处置在任务页“详情”
中进行：可采纳已验收产物（不调用模型）、确定性迁移旧版元数据、定向修复/仅重译中文、重跑
质检、显式全新解析、复查、暂缓、排除与撤销；缺正本 PDF、身份冲突、API 问题与用户修改给出
明确指引而不是假重试按钮。未变化的失败条目不会在每次“检测新增”时自动重发模型；记录的
内容指纹或缺失路径实际变化后才重新纳入，切换模型本身不影响检测；排除偏好保留至用户撤销。
运行前先看 `runtime/python.exe tools/library_sync.py --help`，写入操作需明确 `--apply`。
不要使用旧迁移脚本做首次导入。

发行版不带个人期刊映射或 item pins，但随包提供公共基础缩写表
`app/release/journal_acronyms.base.csv`（常见海洋/地学期刊的文件名/引用键短码，例如
JPO、GRL、JGR、DSR、OS、OM、SciRep、RS、SciData、NC、GMD；这是本工具的命名约定，
**不是** ISO 4 缩写声明）。新安装的个人覆盖表 `data/reports/journal_acronyms.csv` 初始只有
`title,proposed` 表头；基础表 + 个人覆盖共同生效，**个人覆盖优先**，更新版本不会批量改名或
覆盖你的映射。未知期刊在预演中会明确阻塞并列出刊名，可在面板“期刊缩写快捷补表”中按刊保存
（含逗号/引号/Unicode 的刊名会正确转义），保存后重新预演；新映射仍经过键/附件授权核验。
Library 中的 `resources/glossary.md` 初始是空术语表，不包含原作者的科研文献或笔记。
同步收尾可以在 Library 做选择性 Git 提交；缺 Git/仓库/身份配置须先解决，不能把失败显示为完成。

## 离线检查

```powershell
& "$env:LOCALAPPDATA\LibrarySync\app\runtime\python.exe" -B -X utf8 `
  "$env:LOCALAPPDATA\LibrarySync\app\release\offline_smoke.py" --smoke --limit 1
```

另可用 `offline_smoke.py --config` 只读显示安装路径，不读模型凭据。
验收测试还运行六个实际 CLI 的 `--help`。该检查仅适用于刚安装的空台账/空映射状态，使用合成 PDF 验证依赖，禁止网络、
启动子进程、读取 `.secrets`、Zotero prefs 或数据库；不应当作真实单篇同步验收。

## 源码与校验

`source.zip` 仅含显式 allowlist 的通用代码、合成测试和文档；无 `.git` 历史、
个人报告、文献、缓存、凭据或 item pins。`SOURCE_MANIFEST.json` 记录源输入及导出
文件 SHA-256；`source.sha256` 覆盖源码包文件；`SHA256SUMS.txt` 覆盖发行 ZIP/XPI。
Windows 包另有 `app/FILES.json` 和 `bundle.sha256`。

参考 [BUILD.md](release/BUILD.md)、[PRIVACY.md](release/PRIVACY.md)、[发行许可说明](release/DISTRIBUTION_LICENSE.md)
及 [第三方对应源码获取指引](release/THIRD_PARTY_SOURCE_GUIDE.md)。

## 更换全文与模型参数

设置页为 Thinking、推理强度、输出上限和单次超时提供逐模型说明。新配置及显式「按该模型最高值填写」采用已核实的最高参数；习惯预设只填当前角色草稿，不自动保存或启动。读取已有配置不会改写已有选择。

预演之后修改模型不会清空检测结果与逐篇勾选：面板只标记“预演绑定待更新”，保存新配置后自动经 `/v1/preview/rebind` 更新预演的模型快照。重绑严格本机执行，不联网、不查询模型目录、不做同济模型预检；范围、顺序、附件计划、待办与逐篇选择原样保留，原预演的 10 分钟有效期继续计算。重绑会签发新预演编号并作废旧编号：旧窗口或迟到的确认无法用旧编号启动其未确认过的新模型，经旧编号重试会解析到当前预演。开始确认对话框展示本次冻结的模型；`/v1/start` 的配置一致性校验未放宽。本机后台不支持重绑能力时，面板明确提示重新预演并保留逐篇选择。

模型目录按（服务商、端点、协议、凭据代际）独立加载：QC 与 ZH 同身份目录合并为一次远程请求并共享 10 分钟缓存；保存 Key 即作废该服务商旧目录并按新凭据重新获取。选择服务商后默认可信地址自动加载目录，自定义地址仍需显式确认；未保存的草稿 Key 拒绝目录请求。刷新失败保留上次候选并明确标注缓存/失败；界面始终显示来源和获取时间。

DeepSeek 从官方 `/models` 读取调用 ID 和版本展示名，两者分别显示。GLM 从官方公开文档在线获取候选，并区分通用 API 与 Coding Plan；文档候选不表示账户权限。自填地址没有列表接口则保留手填，不套用官方端点目录。

`max_tokens` 是单次输出上限，并非上下文长度。当前已核实 GLM-5.3 / Flash / FlashX 上限 131072、DeepSeek Flash / V4-Pro 上限 393216；均支持最高 `max`。未知模型不猜上限。列表和保存不调用模型，测试按钮才发实际短请求。

预演摘要按当前逐篇翻译选项、原文种类与已有产物，把可执行条目分为“预计需补齐（缺原文/质检/笔记/中译）”与“已有成果待复核”；复核仍需完整 QA 验收，不代表已通过。中文原文与不译条目不要求中译。

若原 PDF 不完整，把全文添加到同一 Zotero 条目，选中这份 PDF 后使用「重新检测 / 更换全文 PDF」并预演确认。旧附件和批注保留为历史版本，旧派生产物可恢复；笔记及其原图保留，新解析图片使用独立目录。SHA-256 相同不重复解析。

删除重加条目仅在精确 DOI 和旧记录状态可核实时换绑；不支持自动迁移变化的 citekey，不凭标题猜测。页数 / 结尾检查是启发式，不能保证出版商全文完整。存在预存个人编辑时先阻断，不能覆盖用户文件。
