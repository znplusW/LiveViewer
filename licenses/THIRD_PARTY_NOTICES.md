# Third-party components

Qt, FFmpeg, libheif and libde265 remain separate replaceable LGPL shared libraries in this onedir distribution. Keep original notices and corresponding source references with redistributed binaries. PyAV is a development-only fixture generator and is not shipped in the application.

Installed versions and original Python distribution licenses:

- PySide6 6.12.0; original notices in python-distributions/PySide6/.
- PySide6_Addons 6.12.0; original notices in python-distributions/PySide6_Addons/.
- PySide6_Essentials 6.12.0; original notices in python-distributions/PySide6_Essentials/.
- shiboken6 6.12.0; original notices in python-distributions/shiboken6/.
- pillow 13.0.0; original notices in python-distributions/pillow/.
- pi_heif 1.4.0; original notices in python-distributions/pi_heif/.
- numpy 2.5.4; original notices in python-distributions/numpy/.

Native library license families and corresponding upstream source:

- Python: PSF-2.0 and included notices. Source: https://github.com/python/cpython
- Qt-PySide6: LGPL-3.0 and third-party notices. Source: https://download.qt.io/archive/qt/6.12/6.12.0/single/
- FFmpeg: Qt build LGPL-2.1-or-later and included permissive third-party notices. Source: https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz
- libheif: LGPL-3.0-or-later. Source: https://github.com/strukturag/libheif
- libde265: LGPL-3.0-or-later. Source: https://github.com/strukturag/libde265
- MinGW-runtime: GCC runtime exception, MIT and BSD; see upstream notices. Source: https://www.mingw-w64.org/

Qt uses its own dynamically linked FFmpeg build. The development-only PyAV installation is excluded from the application.

No runtime component needs to be installed globally. Do not delete or merge these replaceable shared libraries.
