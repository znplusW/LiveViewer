# LiveViewer

**让 iPhone 的实况照片，在 Windows 上继续鲜活。**

LiveViewer 是一款面向 **Windows 11 x64** 的本地 Live Photo 浏览器，尤其适合触屏笔记本。打开从 iPhone 导出的照片文件夹，软件会把同目录、同名的 **HEIC/JPG 与 MOV** 配成一张实况照片：静止时看照片，长按时播放画面与声音，松手回到照片。

**触屏手势 · 独立全屏预览 · 虚拟触控板 · 本地浏览 · GPL-3.0 开源**

## 为看 Live Photo 做的交互

- **手指直接看图**：单指左右滑动切图、双指缩放、双击放大或还原，放大后拖动查看细节；长按播放，松手停止。
- **独立全屏预览窗**：主窗口继续选图，预览窗专注展示。预览初开最大化，可还原和移动，准备好后再进入全屏。
- **整块区域都是虚拟触控板**：通过主窗口的“触控板”控制独立预览，翻页、缩放、平移、长按播放都能操作；支持停靠或浮动。
- **鼠标、触控板、键盘配合使用**：触控板轻触双击拖曳用于平移，明确按住实体按键用于播放；快捷键可以自定义。
- **按文件夹浏览整批照片**：全览、胶片条和目录勾选配合搜索筛选；缩略图按需加载，缓存可放本机、程序目录或随图库保存。
- **选一帧作为新封面**：逐帧预览并保存为新的 HEIC＋MOV 组合，同步更新配对标识和封面时刻；也可单独导出静帧。

浏览、配对分析和缓存均在本机完成，无需账号，也不上传照片。普通图片可以与 Live Photo 一起浏览。

## 下载与使用

前往 [Releases](https://github.com/znplusW/LiveViewer/releases)，选择 Windows x64 绿色版 ZIP。[直接下载 v1.0 绿色版 ZIP](https://github.com/znplusW/LiveViewer/releases/download/v1.0/LiveViewer-Windows-x64-v1.0.zip)。

1. 解压整个 `LiveViewer-Windows-x64-v1.0` 文件夹，运行 `LiveViewer.exe`，保留同目录 `_internal` 文件夹。
2. 点击“添加文件夹”，选择 iPhone 照片的导出目录。图片和配套 MOV 应保留在同一目录，并保持同名。
3. 点击缩略图查看；用鼠标、触屏或默认 `Space` 按住播放，默认 `P` 播放一遍。
4. 想专注看图时打开“独立预览窗”，再按需要进入全屏；打开“触控板”即可从另一操作区控制它。

默认 `F` 显示／隐藏胶片条，`F11` 切换全屏。单击图片不会改变胶片条状态。设置中可调整静音、按住播放方式、自动播放、外观与快捷键。

更多操作见 [使用指南](docs/USER_GUIDE.md) 和随包的 [简明说明](docs/使用说明.txt)。

## 支持范围

- 主要面向 iPhone 导出的 `HEIC/JPG/JPEG + MOV` 双文件 Live Photo，按同目录同名配对。
- 也可浏览 PNG、WebP、BMP、GIF、TIFF、AVIF 等普通图片；当前 GIF 显示首帧，TIFF 显示第一页。
- 支持有声播放，默认静音，可通过声音按钮开启。
- 新封面统一保存为 HEIC。保存副本保留原组；选择覆盖时会更新 HEIC/MOV，成功后清理同名 JPG/JPEG。覆盖无法撤销。
- 不保证 Apple 原生缓入缓出、HDR/Dolby Vision 精确色调或所有第三方回导流程的一致性；视频与照片的裁切、分辨率可能不同。
- 触屏和触控板交互曾在 Windows 11 实体触屏笔记本上逐项验收；不同设备、驱动和多屏环境仍需实际验证。

LiveViewer 当前专注本地浏览与封面选择，不提供 iCloud 同步或 iPhone 直接导入服务。

## 开源与开发

应用采用 **GNU GPL v3.0 only（`GPL-3.0-only`）**，完整条款见 [LICENSE](LICENSE)，版权与明确的组件例外见 [NOTICE](NOTICE)。独立 HEIC 编码 helper 保留 MIT 许可；第三方组件保留各自的许可和声明，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。

- [Windows 原生构建](docs/BUILDING.md)
- [源码分发与依赖来源](docs/SOURCE_DISTRIBUTION.md)
- [v1.0 更新说明草稿](docs/RELEASE_NOTES_v1.0.md)
- [发布准备清单](docs/GITHUB_RELEASE.md)

欢迎通过 [Issues](https://github.com/znplusW/LiveViewer/issues) 反馈问题。请说明系统、设备和复现步骤；分享样本前移除个人信息，尽量使用合成素材。

LiveViewer 为独立开源项目，与 Apple 无关联。iPhone、Live Photos 等名称属于其相应权利人。
