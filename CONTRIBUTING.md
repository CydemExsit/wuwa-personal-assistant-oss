# Contributing

This is an early public, solo-maintained unofficial community tool. Focused bug
reports and fixes are welcome in Chinese or English. Be respectful and discuss
product-policy changes first. We do not claim external adoption.

## Set up and verify — Windows x64 / Python3.12

```powershell
.\scripts\setup.ps1
.\scripts\test.ps1
```

`requirements/windows-py312.lock` pins the tested dev+packaging profile with PyPI
SHA-256 values. Setup installs it, then installs the editable project without
resolving different dependencies. `pip check` must pass. This environment-specific
profile does not promise identical binary output across Windows/Python patch builds.

To update dependencies deliberately, use a disposable environment, install the
desired `.[dev,packaging]` versions, run `pip check`, then run
`python scripts/lock_dependencies.py`. Review the diff and run full tests, native
UI and packaging checks. Keep PySide6 at the verified version until separately
validated. Security tools use `requirements/audit-windows-py312.lock` in an isolated
environment; they are not part of the desktop runtime.

## Scope and data

- Keep OCR/scoring local, uncertainty explicit and manual confirmation available.
- Do not add game automation, process/memory access, injection, game-attached
  overlays/hooks, packet access, game-file modification, cloud OCR or telemetry.
- The existing passive Qt panel is allowed only as the tool's own window with
  click-through/noactivation and capture exclusion, never attached to the game.
- Never commit real screenshots, private OCR outputs, databases, logs, diagnostic
  packages, credentials, personal home paths or proprietary artwork. Generate
  synthetic test inputs; review provenance and licensing for every new asset.
- Do not lower thresholds, remove regressions or invent values to pass tests.

## Pull requests and releases

Use a branch with a focused change. Explain the trigger, resulting behavior,
validation and limitations. Relevant tests and the full gate must pass. Visible
Qt changes need Windows125%/200% checks with synthetic/copied data. The maintainer
reviews product/safety/asset changes before integration.

Commit names/emails become public metadata. Use GitHub noreply if you do not want
to publish a personal email. Do not rewrite others' history.

Release work includes source/resource/native/license audits, startup/offline OCR,
the corresponding-source bundle, branch/main CI, checksums and accurate notes.
No commit should exist merely to inflate apparent activity. See
[dependency licensing](docs/dependency-licensing.md) and [CHANGELOG](CHANGELOG.md).
Contributions to project-authored material are under MIT; upstream rights remain separate.
