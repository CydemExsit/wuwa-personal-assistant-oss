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
return. The unused Mesa software OpenGL fallback is also omitted; this Widgets
interface uses ordinary raster painting. Widgets, native Windows input,
SVG/static-image display and the existing
passive tool panel remain supported.

Original LGPL/GPL/exception and third-party texts are copied from the exact
upstream source archives, together with the original Python dependency notices,
into `_internal/legal/`. Do not remove these notices when sharing the package.

## Corresponding source access

Download `ThirdPartySources-1.1.1.zip` beside the Windows application at the
[v1.1.1 release](https://github.com/CydemExsit/wuwa-personal-assistant-oss/releases/tag/v1.1.1).
It contains complete upstream qtbase, qtsvg, qtimageformats, qttranslations and
pyside-setup archives, including Shiboken, plus Shapely 2.2.0 and its bundled
GEOS 3.14.1 and `SOURCE_MANIFEST.json`. Source
revisions and SHA-256 values are recorded in
[`third_party/upstream-sources.json`](../third_party/upstream-sources.json).
The manifest's immutable official upstream URLs provide an additional way to
retrieve those same sources. The project does not modify these upstream libraries.

Build tools and configuration instructions are in the upstream source archives
and [Qt for Python build documentation](https://doc.qt.io/qtforpython-6/building_from_source/index.html).
Use matching architecture/compiler/Qt/PySide ABI. You can build compatible dynamic
libraries and replace them in a copy of `_internal`, or build/install your modified
PySide environment and run `scripts/build.ps1`. The default dependency lock is a
reproducible installation profile, not a restriction on modified builds.

## Shapely / GEOS

RapidOCR uses Shapely, whose Windows wheel bundles dynamic GEOS libraries under
LGPL-2.1. The original Shapely, GEOS and Windows runtime notices are preserved.
The source bundle includes complete matching Shapely and GEOS archives; the build
rejects a runtime version that differs from the source manifest.

GEOS includes CMake build instructions; see its
[build guide](https://libgeos.org/usage/download/). To use a modified GEOS build,
follow [Shapely's custom GEOS instructions](https://shapely.readthedocs.io/en/stable/installation.html#installation-from-source-with-custom-geos-library)
and build Shapely against it, then package that environment with `scripts/build.ps1`.
The default wheel uses renamed DLLs in `_internal/shapely.libs`; rebuilding the
binding avoids relying on those wheel-specific filenames. No technical or
licensing restriction prevents compatible replacement or debugging this tool
with modified libraries.

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
