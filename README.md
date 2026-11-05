# Library Sync · Zotero 文献同步

Windows x64 上的 Zotero 10.0 插件。选中文献或检测任意分类中的新增条目，预演后在后台整理正本和批注本、解析 PDF、质检，并按需生成段落对照的中文译文。任务页显示验收、索引与本地 Git 提交进度。

## 下载和安装

已有配套后台的用户可以从 GitHub Releases 下载 beta.8 XPI，在 Zotero 的插件管理器中从文件安装。新安装还需要 Windows 后台。以下命令在本机用源码和官方依赖生成完整安装包。准备 Windows x64、Python 3.12 或以上、Git、Zotero 和 Better BibTeX，在下载并解压的源码根目录运行：

```powershell
py -3 -B release/fetch_dependencies.py --apply
py -3 -B tools/build_library_release.py --apply
```

生成的 Windows ZIP 位于 `release/dist`。第三方运行时由构建者从上游下载，构建器逐项校验锁文件中的 SHA-256。GitHub Release 提供插件和源码。项目尚未指定开源许可证。

## 配置 Windows 后台

解压 `library-sync-0.2.0-beta.8-windows-x64.zip` 到普通目录。先预演：

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

在 Zotero 的插件管理界面从文件安装 `app/addon/library-sync-0.2.0-beta.8.xpi`。
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
安装与离线 smoke 没有验证这些真实服务，也没有运行真实模型。
支持暂停、继续未完成步骤及仅重试收尾；CLI 入口提供 `--pause`、`--resume-run`。
运行前先看 `runtime/python.exe tools/library_sync.py --help`，写入操作需明确 `--apply`。
不要使用旧迁移脚本做首次导入。

发行版不带个人期刊映射或 item pins。`data/reports/journal_acronyms.csv` 初始只有
`title,proposed` 表头，未知期刊可能阻塞预演；填写自己的期刊缩写后重试。
随包示例 `journal_acronyms.example.csv` 含公开核实的 Geoscientific Model Development→GMD
映射，可按需复制该行到自己的缩写表。
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

参考 [BUILD.md](release/BUILD.md)、[PRIVACY.md](release/PRIVACY.md)、[LICENSE_PENDING.md](release/LICENSE_PENDING.md)
及 [第三方对应源码获取指引](release/THIRD_PARTY_SOURCE_GUIDE.md)。

## 更换全文与模型参数

设置页为 Thinking、推理强度、输出上限和单次超时提供逐模型说明。新配置及显式「按该模型最高值填写」采用已核实的最高参数；习惯预设只填当前角色草稿，不自动保存或启动。读取已有配置、刷新目录不会改写已有选择。

DeepSeek 从官方 `/models` 读取调用 ID 和版本展示名，两者分别显示。GLM 从官方公开文档在线获取候选，并区分通用 API 与 Coding Plan；文档候选不表示账户权限。界面显示来源和获取时间；读取失败使用缓存或内置目录时明确标记。自填地址没有列表接口则保留手填，不套用官方端点目录。

`max_tokens` 是单次输出上限，并非上下文长度。当前已核实 GLM-5.3 / Flash / FlashX 上限 131072、DeepSeek Flash / V4-Pro 上限 393216；均支持最高 `max`。未知模型不猜上限。列表和保存不调用模型，测试按钮才发实际短请求。

若原 PDF 不完整，把全文添加到同一 Zotero 条目，选中这份 PDF 后使用「重新检测 / 更换全文 PDF」并预演确认。旧附件和批注保留为历史版本，旧派生产物可恢复；笔记及其原图保留，新解析图片使用独立目录。SHA-256 相同不重复解析。

删除重加条目仅在精确 DOI 和旧记录状态可核实时换绑；不支持自动迁移变化的 citekey，不凭标题猜测。页数 / 结尾检查是启发式，不能保证出版商全文完整。存在预存个人编辑时先阻断，不能覆盖用户文件。
