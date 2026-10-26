# Third-party components

Qt, FFmpeg, libheif and libde265 remain separate replaceable LGPL shared libraries in this onedir distribution. The frame-reading FFmpeg/ffprobe subprocess tools and their matching DLLs are isolated from Qt under tools/ffmpeg. Keep original notices and corresponding source references with redistributed binaries. PyAV is a development-only fixture generator and is not shipped in the application.

Installed versions and original Python distribution licenses:

- PySide6_Addons 6.12.0; original notices in python-distributions/PySide6_Addons/.
- PySide6_Essentials 6.12.0; original notices in python-distributions/PySide6_Essentials/.
- shiboken6 6.12.0; original notices in python-distributions/shiboken6/.
- pillow 13.0.0; original notices in python-distributions/Pillow/.
- pi_heif 1.4.0; original notices in python-distributions/pi-heif/.
- numpy 2.5.4; original notices in python-distributions/numpy/.

Native library license families and corresponding upstream source:

- Python: PSF-2.0 and included notices. Source: https://github.com/python/cpython
- Qt-PySide6: LGPL-3.0 and third-party notices. Source: https://download.qt.io/archive/qt/6.12/6.12.0/single/
- FFmpeg: Qt build LGPL-2.1-or-later and included permissive third-party notices. Source: https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz
- FFmpeg-frame-tools: BtbN pinned LGPL shared variant; source/build references and original license in FFmpeg-CLI/. Source: https://github.com/FFmpeg/FFmpeg/tree/2a571b606854520cf89804d8030c8b328e621689
- HEIC-encoder: MIT helper, separate LGPL libheif and BSD Kvazaar shared libraries; exact pins/notices in HEIC/. Source: dependencies/heic-encoder.json and native/heic_encoder/ in the corresponding source ZIP
- ExifTool: Separate ExifTool/Perl subprocess runtime; complete original licenses, source project links and accepted file hashes in ExifTool/. Source: https://exiftool.org/
- MinGW-runtime: GCC runtime exception, MIT and BSD; see upstream notices. Source: https://www.mingw-w64.org/
- libheif: LGPL-3.0-or-later; pi-heif decoder library 1.23.0. Source: https://github.com/strukturag/libheif/tree/v1.23.0
- libde265: LGPL-3.0-or-later; pi-heif decoder library 1.1.1. Source: https://github.com/strukturag/libde265/tree/v1.1.1

pi-heif's original wheel notices are preserved verbatim. The decoder source links above use the native library versions reported by this installed wheel; its bundled notice may refer to older releases. The separate HEIC encoder has its own source pins and provenance in HEIC/.

ExifTool 13.38 and its complete Perl runtime are isolated under tools/exiftool. ExifTool/ retains the original Windows distribution license, bundled Strawberry Perl license archive, launcher notice, and the exact accepted runtime file hashes. Restore the fixed archive using scripts/fetch_exiftool.py; an installed application or PATH is never a product dependency.

Qt uses its own dynamically linked FFmpeg build. Exact frame extraction uses separately pinned FFmpeg/ffprobe executables with their own replaceable libraries; FFmpeg-CLI/ records the checksum, upstream license, build configuration and source references. The development-only PyAV installation is excluded from the application.

No runtime component needs to be installed globally. Do not delete or merge these replaceable shared libraries.
