# Dependencies, notices and rebuilding

Project-authored material is MIT licensed. Library/model licenses remain separate.
Game trademarks, artwork and third-party guides are not relicensed by this project.
No game artwork or private screenshots are distributed in this repository/release.

## Qt / PySide6

The verified runtime is Qt/PySide6 6.11.1. The application uses dynamic Windows
DLLs and PySide bindings, not a static Qt executable. The one-folder layout leaves
these libraries in `_internal/PySide6`; the release places no technical restriction
on replacing compatible library builds or inspecting/rebuilding this software.
The game-safety rules refer to the game, not to your licensing rights in Qt or
this tool. You may inspect and reverse engineer this tool/library as their
applicable licenses permit, including for debugging a modified Qt library.

The generic PyInstaller Qt hooks otherwise include an unused GPL-only virtual
keyboard and unrelated QML, Quick, PDF and video codec libraries. This build
explicitly excludes those optional components. The package audit rejects their
return. Widgets, native Windows input, SVG/static-image display and the existing
passive tool panel remain supported.

Original LGPL/GPL/exception and third-party texts are copied from the exact
upstream source archives, together with the original Python dependency notices,
into `_internal/legal/`. Do not remove these notices when sharing the package.

## Corresponding source access

Download `ThirdPartySources-Qt6.11.1.zip` beside the Windows application at the
[v1.1.1 release](https://github.com/CydemExsit/wuwa-personal-assistant-oss/releases/tag/v1.1.1).
It contains complete upstream qtbase, qtsvg, qtimageformats, qttranslations and
pyside-setup archives, including Shiboken, plus `SOURCE_MANIFEST.json`. Source
revisions and SHA-256 values are recorded in
[`third_party/qt-sources.json`](../third_party/qt-sources.json).
The manifest's immutable official upstream URLs provide an additional way to
retrieve those same sources. The project does not modify these upstream libraries.

Build tools and configuration instructions are in the upstream source archives
and [Qt for Python build documentation](https://doc.qt.io/qtforpython-6/building_from_source/index.html).
Use matching architecture/compiler/Qt/PySide ABI. You can build compatible dynamic
libraries and replace them in a copy of `_internal`, or build/install your modified
PySide environment and run `scripts/build.ps1`. The default dependency lock is a
reproducible installation profile, not a restriction on modified builds.

## Release procedure

1. Install the hashed Windows/Python3.12 profile; run the full quality gate.
2. The build downloads only the pinned official upstream source archives, verifies
   their hashes, preserves complete archives and extracts their license notices.
3. `package_notices.py` includes original dependency license files without editable
   URLs or installation paths. `LICENSE_MANIFEST.json` records their hashes.
4. Build the app; independently audit source/resources/native DLLs/legal files and
   OCR model hashes, then run offline OCR and isolated startup smoke tests.
5. Publish the Windows ZIP, corresponding-source ZIP and their checksums together.
   Keep the source archive available alongside the matching binary release.

Installation/build-time dependency downloads and optional public-announcement
updates are distinct from OCR: game frames and user databases stay local.
