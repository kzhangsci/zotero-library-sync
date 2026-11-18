# 第三方许可与对应源码获取

自有源码采用 **MIT**；集成 PyMuPDF/MuPDF 的组合发行遵守 AGPL 条件。
详见 DISTRIBUTION_LICENSE.md。发行前应核对下列对应源码、构建资料及通知均已提供。

本包使用 PyMuPDF **1.26.7**（官方 wheel），其 MuPDF 版本为 **1.26.12**。
公开许可证为 AGPL v3，另有 Artifex 商业许可选项；未获得或声称获得商业许可。
wheel 的 `COPYING` 仅有简短双许可声明；本项目另在 `release/AGPL-3.0.txt` 提供完整 AGPL 正文，并保留包内第三方原始通知。

对应源码入口：

- [PyMuPDF 1.26.7 官方 PyPI 源码与元数据](https://pypi.org/pypi/PyMuPDF/1.26.7/json)
- [PyMuPDF 1.26.7 source archive](https://files.pythonhosted.org/packages/48/d6/09b28f027b510838559f7748807192149c419b30cb90e6d5f0cf916dc9dc/pymupdf-1.26.7.tar.gz)
- [MuPDF 1.26.12 官方源码](https://mupdf.com/downloads/archive/mupdf-1.26.12-source.tar.gz)
- [PyMuPDF 构建说明](https://pymupdf.readthedocs.io/en/latest/installation.html)

PyMuPDF 源码 SHA-256：
`71add8bdc8eb1aaa207c69a13400693f06ad9b927bea976f5d5ab9df0bb489c3`。
本轮从 PyPI 官方发行文件下载并核验了该源码包；它的 `setup.py` 指定上述 MuPDF
版本和官方源码 URL。源码包内包含构建脚本与公开测试，MuPDF 另按上方地址获取。
不能把只下载 PyMuPDF Python 包解释为已归档全部 MuPDF 对应源码。

其余实际依赖及许可：

| 依赖 | 固定版本 | 上游许可 |
|---|---|---|
| CPython | 3.13.17 | PSF-2.0 及其自带第三方 notices |
| Requests | 2.34.2 | Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| PyYAML | 6.0.3 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| charset-normalizer | 3.5.2 | MIT |
| idna | 3.20 | BSD-3-Clause |

这些许可只属于各依赖，不是 Library Sync 项目许可证。
每个版本的官方 PyPI JSON URL、wheel URL、SHA-256 均在 `dependencies.lock.json`。
对应项目的该版本 PyPI 元数据 `urls` 中 `packagetype=sdist` 提供源码及校验值。
CPython 对应源码与嵌入第三方组件信息见
[Python 3.13.17](https://www.python.org/downloads/release/python-31317/) 与
[官方 Windows SPDX SBOM](https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip.spdx.json)。

本构建未修改这些官方 wheel 的库代码，仅解包并保留 notices；CPython `_pth` 和本项目
`sitecustomize.py` 是安装配置。提供对应源码/构建方式并处理许可证兼容性是正式发布前的
待确认事项，本地技术测试不能替代它。


macOS 新增 psutil 7.1.3（BSD-3-Clause）；依赖版本和官方 wheel SHA-256 见 dependencies.macos.lock.json。
macOS 不分发 CPython 本体，安装器使用用户安装的 CPython 3.13。
