HEIC cover export uses libheif (LGPL-3.0-or-later) and Kvazaar (BSD-3-Clause).
These shared libraries are replaceable in _internal/tools/heic. No x265 is used.
Corresponding source URLs and SHA-256 checksums: provenance.json.
Full reproducible build recipe and helper source are in the accompanying source ZIP:
scripts/build_heic_encoder.py, dependencies/heic-encoder.json, native/heic_encoder/.
You may replace these libraries, rebuild the helper, and debug your modifications.
