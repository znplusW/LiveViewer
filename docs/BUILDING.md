# Windows 原生构建

使用 Windows 11 x64、Python 3.12 x64、Git 和 PowerShell。HEIC 原生 helper 还需要 CMake、Ninja 和 MSVC C++ 构建工具；详见 `scripts/build_heic_encoder.py` 的前置检查。不要复用其他平台的虚拟环境。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts/fetch_frame_tools.py
.\.venv\Scripts\python.exe scripts/build_heic_encoder.py
.\.venv\Scripts\python.exe scripts/fetch_exiftool.py --archive 'C:\Path\exiftool-13.38_64.zip'
.\.venv\Scripts\python.exe scripts/generate_fixtures.py --output tests/fixtures --require-codecs
.\.venv\Scripts\python.exe launch.py
```

ExifTool 的版本、归档 SHA-256 和完整运行文件清单在 `dependencies/exiftool-windows.json`。当前固定版本为 13.38；公共下载入口仍需在正式发布前落实，不能用任意新版本替代。其他原生依赖按 `dependencies/` 的固定来源与 SHA-256 获取。

## 按影响范围验证

局部修改运行对应测试；涉及输入、播放、封面保存或窗口生命周期时补相关专项。正式定版执行完整单元回归、原生窗口属性读回与关闭清理、源码和冻结程序自检及实际启动。会操作本机窗口或光标的测试应先确认无人使用前台；符号链接测试可能需要 Windows 相应权限。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m tests.native_edge_probe --report reports/native-edge-probe.json --expect-platform windows
.\.venv\Scripts\python.exe -m unittest tests.test_native_fullscreen_gesture.NativeReadbackTests -v
.\.venv\Scripts\python.exe launch.py --self-test --fixtures tests/fixtures --report-dir reports/source
.\.venv\Scripts\python.exe scripts/build_windows.py --build-only
.\.venv\Scripts\python.exe scripts/verify_bundle.py
```

源码/EXE 的 Windows 原生自检应使用 `QT_QPA_PLATFORM=windows`；离屏单测应显式使用 `QT_QPA_PLATFORM=offscreen`，两者不能互相替代。冻结程序也需执行同一 `--self-test` 并检查实际退出码和报告，再完成正常启动/退出检查。测试资料与用户设置放隔离目录，避免写进发行包。

构建结果位于 `dist/LiveViewer/`。确认最终验证和许可/源码分发完成后，运行 `scripts/build_windows.py --archive-only` 生成程序和源码 ZIP。对应源码说明见 [SOURCE_DISTRIBUTION.md](SOURCE_DISTRIBUTION.md)。

GitHub Actions 的构建工作流仅手动触发，产物上传到 Actions artifacts；这一步不会自动创建公开 Release。正式发布应记录源码提交、标签、构建结果与附件校验值之间的对应关系。
