# GitHub 分发与维护

稳定版本使用 Windows x64 绿色 ZIP，解压完整目录后运行 LiveViewer.exe，不提供安装器。版本标签、源码与程序附件应一一对应；附上 SHA256SUMS.txt。

## 公开内容

- windows-native 为 Windows 实体机开发线的公开源码入口，历史 main 开发线保留。
- LiveViewer-Windows-x64-v1.0.zip 是普通用户的下载入口。
- 同一 Release 提供匹配源码、第三方源码获取清单及开发者可选的固定 ExifTool 归档。
- 不发布本地工作日志、私有验证目录、账号数据或用户照片。

## 后续维护

小改动按实际影响范围验证。正式定版执行完整回归、原生窗口属性与关闭清理、源码及冻结程序自检和正常启动退出。源码与构建脚本在 BUILDING.md，第三方对应源码在 SOURCE_DISTRIBUTION.md。

发布前核对版本/提交与全部附件 SHA，保留既有历史标签。GitHub Actions 手动构建只生成 artifacts，不自动公开 Release。修改软件前先建立可恢复的 Git 检查点。
