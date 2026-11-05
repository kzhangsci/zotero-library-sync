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
```

依赖仅从 Python 官方站和 PyPI 项目发行文件获取。锁文件固定具体版本、wheel 和
官方 SHA-256；下载器不读取 pip 配置/密钥，不使用含凭据 URL，不从第三方镜像获取。
保留 `release/vendor` 即可离线重建。`--source-only` 仅构建 XPI 与源码包。

`release/export_rules.py` 是正向 SOURCE_ALLOWLIST，逐个文件读取；不执行整仓复制后排除。
每项导出记录输入/输出哈希，额外文件不会被带入。发布源码使用独立数据目录和空的历史映射。构建器按清单复制源码并记录哈希。新增模块需要纳入清单，并通过导入和离线安装测试。

支持 Windows x64 / Zotero 10.0。嵌入式 Python 的 `_pth` 仅包含包内标准库、
site-packages、tools 和 release，关闭用户 site/PYTHONPATH 继承；不安装 pip。
依赖原 notice 和 DLL 随包保留。Windows/系统组件本身不是本包分发内容。

版本标签和 manifest 均为 `0.2.0-beta.8`；构建检查 manifest 契约，安装版 Zotero
schema 检查另记录在本地验收报告中。`.invalid` 更新源表示手动更新，不能宣称更新服务就绪。
所有 ZIP 使用固定时间戳和排序；相同输入/依赖得到相同 bytes。源码包没有 Git 历史。

公开的 Release 提供源码和 XPI。完整 Windows 包在使用者本机生成。
