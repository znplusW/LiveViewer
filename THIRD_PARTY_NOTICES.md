# LiveViewer third-party notices

The LiveViewer application is licensed under GNU GPL version 3 only (`GPL-3.0-only`); see `LICENSE` and `NOTICE`. The separately built HEIC helper in `native/heic_encoder/` remains MIT-licensed; its full notice and the notice retained from earlier application versions are in `licenses/LiveViewer-MIT-NOTICE.txt`.

The portable binary contains separately licensed components. Original license texts,
installed versions, native library inventory and corresponding upstream source
references are retained in `licenses/THIRD_PARTY_NOTICES.md` and
`licenses/DEPENDENCIES.json`. Keep the complete `licenses` directory when sharing
the application. Qt, its FFmpeg build, libheif and libde265 are distributed as
replaceable dynamic libraries; their LGPL rights and notices remain applicable.

The development-only PyAV encoder stack is not included in the application.

Exact frame reading uses a separate, SHA-256-pinned LGPL shared FFmpeg/ffprobe
distribution under `_internal/tools/ffmpeg`. Its original license, checksum,
binary inventory and corresponding source/build references are retained under
`licenses/FFmpeg-CLI`. Keep these replaceable DLLs separate from Qt's libraries.

HEIC cover writing uses the MIT-licensed native helper with separate libheif
(LGPL) and Kvazaar (BSD) libraries under `_internal/tools/heic`. Original notices,
source pins and native binary hashes are retained under `licenses/HEIC`; the
helper source and build recipe are in the corresponding source ZIP.

Public binary releases must provide access to the exact corresponding source of the application and bundled copyleft dependencies, including relevant build recipes and modifications. See `docs/SOURCE_DISTRIBUTION.md`; a project homepage alone is not proof of matching source availability.
