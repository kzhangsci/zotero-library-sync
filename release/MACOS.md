# macOS 安装与验收

beta.16 为 macOS 首版。目标为 macOS 13+，Apple Silicon arm64 与 Intel x86_64。
当前兼容性/实机测试结果以随版本验收报告为准；不能把 Windows 上的模拟测试当作 Mac 实机验收。
XPI 与 Windows 共用。Mac 后台包提供两种架构的固定版本 wheel，不包含 Python 本身。

## 安装

1. 安装 python.org 的 CPython 3.13，以及 Git。使用 macOS 对应的 Zotero 10.0。
2. 解压 `library-sync-0.2.0-beta.16-macos.zip`。在终端进入解压目录。
3. 先预演（替换你自己的路径、基准日期和 Git 身份）：

```bash
./setup-macos.command --library-dir "/Users/yourname/Documents/Library" \
  --baseline-date 2026-01-01 --init-library-git \
  --git-user-name "Your Name" --git-user-email "you@example.org"
```

确认预演后，在同一命令末尾添加 `--apply` 安装。已有 Library Git 仓库时不传
`--init-library-git`。安装器仅配置该 Library 和独立 data 的 Git，不创建 remote，
不提交已有内容，不读取模型密钥，不启动同步。安装器在目标 app 下新建专用虚拟环境，
仅从随包 wheel 离线安装，使用 SHA-256 严格校验，不混入系统/其他项目 Python 包。

默认安装目录为 `~/Library/Application Support/LibrarySync/`，其中 `app` 存程序，
`data` 存用户状态。安装器拒绝覆盖已有 app/data；本版不自动执行跨版本数据迁移。
安装失败时保留现场，`INSTALL_RECEIPT.json` 的 `runtime_ready=false` 表示依赖尚未就绪。
首次安装无参数双击脚本只显示用法，不擅自选择 Library 或开始同步。

4. 从 Zotero 插件管理器安装 XPI，打开 Library Sync 面板。填写：

- 程序目录：`~/Library/Application Support/LibrarySync/app` 的完整绝对路径。
- Python：上面目录下的 `runtime/bin/python3` 完整绝对路径。

默认位置可自动发现；自定义目录需手动填写。路径支持空格和中文。
保存路径后连接后台，再填写模型与 MinerU 设置。后台会在需要时请求系统 Keychain 授权。

## 密钥和恢复

Mac 密钥保存在系统 Keychain，`.secrets/*.keychain` 只记录随机引用。桥接描述符
目录和文件分别限制为当前用户的 0700/0600。Keychain 拒绝或被锁定时操作明确失败，
没有明文降级。Windows 的 DPAPI 文件不能移到 Mac 使用，需要在 Mac 重新输入密钥。

任务锁使用 POSIX 文件锁，进程核验同时比对 PID、创建时间和可执行程序路径。
Git 提交使用经 QA 核验的内容快照与独立索引；不会在提交时重新读取编辑器刚改动的
工作文件，也不会消费用户原先的暂存区。原有暂存、并发索引锁或身份不明时停止。
任务运行期间使用临时 caffeinate 子进程防空闲休眠，不修改系统电源设置。

## 许可证

自有源码采用 MIT。集成 PyMuPDF/MuPDF 的组合发行遵守 AGPL 条件，第三方组件
保留各自许可。详见 `app/release/DISTRIBUTION_LICENSE.md` 与第三方源码指南。

## 实机验收项目

- 安装预演与正式安装、中文/空格路径、重复安装保护、升级保留状态。
- Keychain 保存、读取、拒绝访问，以及单篇模型设置配置独立。
- 任务锁排他、进程退出/PID 变化、暂停恢复、用户暂存区和晚到修改保护。
- PDF 页数/批注处理、MinerU 上传副本生成（模拟解析，不提交真实文献）。
- Zotero 菜单、面板启动与重开、本机桥接连接；实际模型调用须单独授权。
