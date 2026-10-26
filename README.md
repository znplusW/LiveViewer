# LiveViewer

**让 iPhone 的实况照片，在 Windows 上继续鲜活。**

LiveViewer 是一款面向 **Windows 11 x64** 的本地 Live Photo 浏览器，尤其适合触屏笔记本。打开从 iPhone 导出的照片文件夹，软件会把同目录、同名的 **HEIC/JPG 与 MOV** 配成一张实况照片：和iOS相册一样，静止时看照片，长按播放实况。

**触屏手势 · 独立全屏预览 · 虚拟触控板 · 本地浏览 · GPL-3.0 开源**

## 为看 Live Photo 做的交互

- **为触屏设备优化**：单指左右滑动切图、双指缩放、双击放大或还原，放大后拖动查看细节；长按播放，松手停止。
- **独立全屏预览窗**：选图和看图窗口可以分别放到不同的显示器上，适配多屏看图环境。
- **虚拟触控板**：触屏设备可以使用虚拟触控板进行操作。
- **更改关键帧**：逐帧预览并保存为新的 HEIC＋MOV 组合，同步更新配对标识和封面时刻；也可单独导出静态帧。

浏览、配对分析和缓存均在本机完成，无需账号，也不上传照片。普通图片可以与 Live Photo 一起浏览。

## 下载与使用

前往 [Releases](https://github.com/znplusW/LiveViewer/releases)，选择 Windows x64 绿色版 ZIP。[直接下载 v1.0 绿色版 ZIP](https://github.com/znplusW/LiveViewer/releases/download/v1.0/LiveViewer-Windows-x64-v1.0.zip)。

1. 解压整个 `LiveViewer-Windows-x64-v1.0` 文件夹，运行 `LiveViewer.exe`，保留同目录 `_internal` 文件夹。
2. 点击“添加文件夹”，选择目录。图片和配套 MOV 应保留在同一目录，并保持同名。
3. 点击缩略图查看；用鼠标、触屏或默认 `Space` 按住播放，默认 `P` 播放一遍。

默认 `F` 显示／隐藏胶片条，`F11` 切换全屏。

更多操作见 [使用指南](docs/USER_GUIDE.md) 和随包的 [简明说明](docs/使用说明.txt)。

## 支持范围

- 主要面向 iPhone 导出的 `HEIC/JPG/JPEG + MOV` 双文件 Live Photo，按同目录同名配对。
- 也可浏览 PNG、WebP、BMP、GIF、TIFF、AVIF 等普通图片；当前 GIF 显示首帧，TIFF 显示第一页。
- 触屏和触控板交互曾在 Windows 11 实体触屏笔记本上逐项验收；不同设备、驱动和多屏环境仍需实际验证。
LiveViewer 当前专注本地浏览与封面选择，不提供 iCloud 同步或 iPhone 直接导入服务。
iPhone导出或导入照片，可以使用爱思助手。

## 开源与开发

应用采用 **GNU GPL v3.0 only（`GPL-3.0-only`）**，完整条款见 [LICENSE](LICENSE)，版权与明确的组件例外见 [NOTICE](NOTICE)。独立 HEIC 编码 helper 保留 MIT 许可；第三方组件保留各自的许可和声明，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。

- [Windows 原生构建](docs/BUILDING.md)
- [源码分发与依赖来源](docs/SOURCE_DISTRIBUTION.md)
- [v1.0 更新说明草稿](docs/RELEASE_NOTES_v1.0.md)
- [发布准备清单](docs/GITHUB_RELEASE.md)

LiveViewer 为独立开源项目，与 Apple 无关联。iPhone、Live Photos 等名称属于其相应权利人。
