# Third-party software and assets

The project-authored code, documentation and geometric app icon are MIT licensed.
That license does not replace upstream software/model licenses or grant rights in
Wuthering Waves names, trademarks, game content or third-party articles.

This OSS edition redistributes no game portraits, announcement images, private
OCR samples or game screenshots. Character identification uses local initials;
public game facts and guide references remain attributed to their linked sources.
The software is an unofficial community project, independent of KURO GAMES.

Important components:

| Component | Terms / notices |
| --- | --- |
| Python 3.12 | PSF license and its included third-party notices |
| PySide6 / Shiboken / Qt 6.11.1 | Open-source LGPL/GPL alternatives as stated by upstream; this Widgets build uses LGPL-compatible Qt components and Qt translation exceptions |
| RapidOCR 3.9.2 | Apache-2.0, RapidOCR Authors |
| Three PaddleOCR-derived ONNX models | Apache-2.0, Baidu / PaddleOCR rights holders; separate [model notice](third_party/PaddleOCR-MODEL-NOTICE.txt) |
| ONNX Runtime | MIT and Microsoft third-party notices |
| OpenCV | Apache-2.0 plus its bundled third-party notices; unused FFmpeg codec DLLs are excluded |
| NumPy / bundled numerical libraries | NumPy and its bundled upstream license texts |
| Other Python dependencies | Original license texts collected from the exact build environment |

The Windows package contains original license files and a hash inventory in
`_internal/legal/`. This includes the Python license, upstream Qt/PySide licenses,
model provenance, dependency notices and corresponding-source manifest.
The inventory covers the build environment; it does not claim that every listed
developer tool is shipped as executable code.

Each binary release also includes `ThirdPartySources-Qt6.11.1.zip`: complete
upstream archives for the exact Qt/PySide source revisions used. Read
[dependency licensing and rebuilding](docs/dependency-licensing.md) for source
access, dynamic library replacement and the release verification procedure.

Upstream references: [Qt obligations](https://www.qt.io/development/open-source-lgpl-obligations),
[Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html),
[RapidOCR model notice](https://rapidai.github.io/RapidOCRDocs/main/en/model_licenses/).
