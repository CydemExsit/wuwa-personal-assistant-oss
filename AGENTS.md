# Repository instructions

Read README, CONTRIBUTING, SECURITY and relevant design docs before changes.

- Preserve local OCR/scoring, no full live-frame persistence, explicit uncertainty
  and manual correction. Never add game automation, process/memory access,
  injection, game-attached overlays/hooks, game-file changes, packet access or cloud OCR.
- The existing passive Qt panel is permitted only as the tool's own window, with
  capture exclusion, noactivation and click-through. Do not broaden native API exceptions.
- Use synthetic tests. Never commit private samples, databases, logs, credentials,
  personal home paths or game artwork. Review assets and commit metadata.
- Keep changes focused. Run scripts/test.ps1, public_safety_audit.py and the redacted
  history scan; visible Qt changes require native125%/200% verification.
- Never weaken tests or invent adoption, measurements or maintenance activity.
- Follow the maintainer's authorized delivery scope. External contributions end
  in reviewable PRs. Do not change visibility, rewrite history, force-push, publish
  releases or merge others' work without maintainer authorization.
- An authorized release requires branch/main CI, source/resource/native/license
  audits, startup/offline OCR, corresponding sources and checksums before completion.

Long work may use ignored .codex/WORK_PROGRESS.md. Keep private audit evidence
ignored, preserve unrelated work, and do not treat a checkpoint as completion.
