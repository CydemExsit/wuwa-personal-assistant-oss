# 1.1.3

GPU-accelerated local OCR and bounded non-echo probing. See
[gpu-ocr-performance.md](gpu-ocr-performance.md) for measurements, resource policy
and fallback controls. Windows packages bundle DirectML.

Validation: 1507 tests passed, 3 expected skips; Ruff, formatting, mypy,
dependency and safety checks passed. Packaged GPU-required and CPU OCR self-tests
passed on RTX4070. Installer editions also passed Unicode-path installation,
shortcuts, reinstall, uninstall and data-retention checks.
