# 第三方许可与对应源码获取（v1.0）

LiveViewer 应用采用 GPL-3.0-only，独立 HEIC helper 保留 MIT。第三方组件保持原作者的许可，原始文本完整随程序分发。本清单补充源码取得位置，不改变这些原始许可证。

## 构建本版

同一 Release 的 LiveViewer-Source-v1.0.zip 是本应用匹配源码；构建步骤在其中 docs/BUILDING.md。绿色版用户无需安装开发依赖。

ExifTool 使用固定的 13.38 运行归档。同一 Release 提供 [ExifTool-13.38-pinned-runtime.zip](https://github.com/znplusW/LiveViewer/releases/download/v1.0/ExifTool-13.38-pinned-runtime.zip)；SHA-256：828fdcf2d229b88e1b327e1043ac33974fb15787e156d44fc61a94286f40eb16。此文件是本项目已核验的固定归档，不冒称为官方原 ZIP；仅含 pin 中的508个原始运行文件，逐文件SHA一致，无额外文件。

开发者下载后向 scripts/fetch_exiftool.py 的 --archive 参数提供该路径；Actions 的 exiftool_archive_url 参数可填写此附件发布后的 HTTPS 地址。该附件仅供开发者复现构建；普通用户无需单独下载。

## Qt、PySide 与视频库

- PySide/shiboken 6.12.0：https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz
  SHA-256：099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751。本轮已实际下载校验，归档包括双方源码和构建脚本。
- Qt 6.12.0：https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz
  上游公布 SHA-256：98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165。已核对官方 URL 和校验信息；本轮未下载443MB完整归档，不将官方哈希说成本地实测哈希。
- Qt 内 FFmpeg 9.0.1：https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.gz
  已下载 SHA-256：657dbf258cce6c0681714b6e40b8ca69e988c67ecfc2f7aab47574e6ebaceb38，归档SHA1与Qt v6.12.0固定配方相符。
  Windows 配方：https://raw.githubusercontent.com/qt/qt5/v6.12.0/coin/provisioning/common/windows/install-ffmpeg.ps1
  配置：https://raw.githubusercontent.com/qt/qt5/v6.12.0/coin/provisioning/common/shared/ffmpeg_config_options.txt
- 独立 FFmpeg/ffprobe 的源 commit 和构建配方 commit：见源码包 dependencies/ffmpeg-windows.json 和程序 licenses/FFmpeg-CLI/，保留其 LGPL 共享库及上游原文。
- HEIC encoder 的 libheif/Kvazaar 源归档、SHA与完整构建配方：见 dependencies/heic-encoder.json、native/heic_encoder/、scripts/build_heic_encoder.py 和 licenses/HEIC/。
- pi-heif 解码用 libheif/libde265 的实际版本与源码：见 licenses/DEPENDENCIES.json 的 pi_heif_decoder_sources，原 wheel notice 原样保留。

## 独立 ExifTool、启动器与 Perl

ExifTool/Perl 是隔离的标准 CLI 运行时，不因主应用采用GPL就被改为GPL-only。ExifTool 13.38 原README及作者明确提供 Perl Artistic 或 GPL 选项；本项目保留这些原始选项和完整声明。Perl核心同样有原包提供的Artistic/GPL选项，其他随包组件仍依各自许可证；不把所有组件合并声明成同一个许可。

- ExifTool 13.38 源码：https://github.com/exiftool/exiftool/tree/13.38
  已取官方tag归档SHA-256：e27fd9bc6cec3239b5eaa83aaa92b77e55f90783c0eaaabe88f671bb58588b40；Windows入口脚本与固定运行文件匹配。
- 作者关于许可选项的说明：https://github.com/exiftool/exiftool/issues/321
- Oliver Betz 启动器：随包readme声明CC0；静态版本1.6、产品版本13.38。作者源码：https://oliverbetz.de/cms/files/Artikel/Portable-Perl-Applications/ppl.zip
  已下载SHA-256：72a112caac70de4cab56971d0bf1ea8050f8228ed036ec88e7657d40cc08ce0e。不承诺该源码可逐字节重建本地启动器。
- Perl静态版本与Strawberry Perl 5.32.1.1 x64匹配。官方版本记录：https://strawberryperl.com/release-notes/5.32.1.1-64bit.html
  官方原始portable发行：https://strawberryperl.com/download/5.32.1.1/strawberry-perl-5.32.1.1-64bit-portable.zip
  官方公布SHA-256：692646105b0f5e058198a852dc52a48f1cebcaf676d63bbdeae12f4eaee9bf5c。本轮未下载此158MB原发行包。

上游源码和构建入口分别提供，不将本应用的源码ZIP说成包含全部第三方源码。发布者需维护这些获取入口；若上游链接失效，应补匹配镜像。本清单不声称二进制可逐字节复建，也不修改原始版权或授权条款。
