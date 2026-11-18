# 可复现的本地构建

所有构建默认 dry-run；明确 `--apply` 才写入。构建不运行 Git、不创建 remote、不发布。
`--limit 0/1` 表示一个完整 release；依赖获取的 `--limit N` 限制本次下载数量，
未齐备的依赖不能产生完整 Windows 包。测试命令只在临时目录写合成数据。

```powershell
py -3 -B release/fetch_dependencies.py
py -3 -B release/fetch_dependencies.py --apply
py -3 -B tools/build_library_release.py
py -3 -B tools/build_library_release.py --apply
py -3 -B -m unittest discover -s tools -p test_library_release.py
node --test tools/zotero_library_sync/tests/runtime.test.cjs
node --test tools/zotero_library_sync/tests/ui-beta15.test.cjs
py -3 -B -m pytest tools/test_library_control_beta15.py tools/test_plugin_bridge_beta15.py -q
```

依赖仅从 Python 官方站和 PyPI 项目发行文件获取。锁文件固定具体版本、wheel 和
官方 SHA-256；下载器不读取 pip 配置/密钥，不使用含凭据 URL，不从第三方镜像获取。
保留 `release/vendor` 即可离线重建。`--source-only` 仅构建 XPI 与源码包。

`release/export_rules.py` 是正向 SOURCE_ALLOWLIST，逐个文件读取；不执行整仓复制后排除。
每项导出记录输入/输出哈希，额外文件不会被带入。明确转换包括：

- `library_paths.LEGACY_DEFAULTS` 清空；路径常量改用通用配置。
- 历史 `PINS`、`EXPLICIT_RENAMES` 清空；删除模块/函数历史叙述与注释。
- `library_resume.LEGACY_RUN=None`、`LEGACY_PAPERS={}`，发行版拒绝没有所有权基线的历史专用恢复。
- `intake_new` 仅导出当前 API intake 所需常量和缩写读取函数，旧 daemon 流程不发行。
- 旧 GLM/Codex/Claude CLI 适配器导出为明确报错的兼容入口；发布版采用用户配置的 API。
- 凭据默认目录、并发锁目录、校验报告目录在原模块直接接入路径层，指向 DATA；代码根 REPO 不重定义为数据根。

这些转换仅作用于导出字节，个人 checkout 保留兼容行为。对 export 再扫描个人路径、
已知 item pins、私钥和长 token 形态；不能把这当成未知秘密的数学证明。
新增运行时模块必须先加入 allowlist，并复查 AST 导入闭包；不自动扩大导出范围。
发行包包含独立的合成暂停/恢复检查及 `test_library_progress`、`test_qc_protocol`、
`test_model_pipeline` / `test_model_catalog`，在导出运行时上执行，不依赖私人控制器历史 fixture。

支持 Windows x64 / Zotero 10.0。嵌入式 Python 的 `_pth` 仅包含包内标准库、
site-packages、tools 和 release，关闭用户 site/PYTHONPATH 继承；不安装 pip。
依赖原 notice 和 DLL 随包保留。Windows/系统组件本身不是本包分发内容。

版本只有一个来源：`tools/zotero_library_sync/manifest.json`。XPI 构建器、面板资源
`?v=`、面板显示版本、对话框 URL、诊断版本与 RELEASE.json 全部由它生成或在打包前
强制比对，任何不一致都会在产出包之前失败（当前为 `0.2.0-beta.16`）；构建检查
manifest 契约，安装版 Zotero schema 检查另记录在本地验收报告中。`.invalid`
更新源表示手动更新，不能宣称更新服务就绪。
所有 ZIP 使用固定时间戳和排序；相同输入/依赖得到相同 bytes。源码包没有 Git 历史。

正式公开前仍需确定许可证、完成获授权的干净 Windows/Zotero 单篇真实服务验收，
以及明确更新策略。当前任务只制作可审阅私有包。


## macOS 构建

先执行 `python3 release/fetch_macos_dependencies.py --apply`，再用
`python3 tools/build_library_release.py --source-only --macos --apply`。
macOS 包包含固定哈希的 arm64/x86_64 wheels，要求本机 CPython 3.13，安装时离线创建专用环境。
原生验收工作流为 `.github/workflows/macos-beta16.yml`；真实系统 Keychain、文件锁和 Zotero 检查在临时数据和测试钥匙串中进行。
