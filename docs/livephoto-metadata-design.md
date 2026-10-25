# Live Photo 封面保存验证

实现依据：

- [Apple WWDC 2016 Session 501](https://developer.apple.com/videos/play/wwdc2016/501/)：照片与 QuickTime 使用相同 UUID，视频另有单个样本的封面时间轨道。
- [Apple livePhotoMovieMetadata](https://developer.apple.com/documentation/avfoundation/avcapturephotosettings/livephotomoviemetadata)：MOV content identifier 与照片关联。
- [Apple Photos 保存 Live Photos](https://developer.apple.com/documentation/avfoundation/capturing-and-saving-live-photos)：`.photo` 和 `.pairedVideo` 作为同一 Photos 资产的资源。
- [ExifTool Apple 原始解析器](https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/Apple.pm)：MakerNote 0x0011 即 ContentIdentifier。此实现不运行或打包 ExifTool。
- [爱思助手官方实况导入说明](https://www.i4.cn/news_detail_20061.html)：在专用实况导入入口成对提供同名 JPG/HEIC 和 MOV。

图片保持 JPEG/HEIC 格式。MakerNote 使用 Apple iOS 头、big-endian IFD 与 tag17 字符串；EXIF MakerNote 类型必须为 UNDEFINED(7)，不是 Pillow 对嵌套 bytes 默认写出的 BYTE(1)。实际 ImageIO 验证可区分这两种情况。

视频保留原 A/V 轨道、压缩数据和偏移，旧 moov 标为空闲，追加新的索引与 metadata 样本；不重编码音视频。每次输出新的配对 UUID，更新独立 mebx/still-image-time 轨道；时间来自实际解码帧 PTS，包含起始时间，按影片时间基准定位，不按估计 FPS 计算。保留其他元信息。无法安全修改的结构直接拒绝。

保存先暂存并校验两个文件。覆盖使用临时恢复材料；成功删除，失败恢复，恢复受阻则保留材料并提示。两个文件不存在跨文件系统原子替换保证；不得将两次 replace 描述为原子事务。

验证层级：

1. 单元测试验证 JPEG/HEIC 标识、封面时刻、VFR、旋转视频、重复修改与失败处理。
2. ffprobe 独立读取音视频 packet 的 SHA256、PTS/DTS、时长，确认修改前后压缩内容与时间轴不变。
3. Windows 源代码和打包 EXE 自检读取新图片与 MOV 并核验实际封面时间。
4. macOS 可运行 `scripts/verify_apple_livephoto.swift`，用 ImageIO、AVFoundation、PHLivePhoto 检查同一对文件；仅只读，不导入相册。
5. iPhone 最终识别仍需通过具体导入工具实测；不能把 macOS 框架验证称为 iPhone 端实测。
