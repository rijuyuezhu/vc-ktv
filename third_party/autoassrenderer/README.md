# Bundled AutoASSrenderer4karaOK processing engine

Source: https://github.com/Volta233/AutoASSrenderer4karaOK
Pinned source commit: 39cedb317c9e11add83e9a72c31368cabd8416d4
License: Apache License 2.0 (see LICENSE in this directory).

The portable upstream model processing components have been copied to top-level
`ktv_app/` with their original module names/import structure so fixes can be
compared against upstream. This is a **library subset**, not the upstream web
application. The upstream `ktv_app/lyrics.py` is intentionally reduced to its
original lyric preprocessing functions; we do not import their online lyrics
retrieval, GUI, DB, installer, or task scheduler.

Upstream is credited as the author of the processing code. New VC KTV-specific
glue goes in `vc_ktv/upstream_pipeline.py`; avoid editing original algorithm
modules without a documented ARM64/integration reason. Model/weight and
third-party package licensing is **separate** from this source license.
