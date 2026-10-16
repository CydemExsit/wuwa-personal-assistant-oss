# Security and privacy reports

The maintained OSS line is1.1.x. Report the exact version/commit. Earlier private
development binaries are not the public release line. This solo-maintained
community project has no guaranteed response SLA.

For credentials, privacy leaks or unsafe game interactions, use GitHub's
**Security → Report a vulnerability** after private reporting is enabled. Do not
put secrets/exploit details into a public issue. If that channel is unavailable,
open a minimal issue asking for a private channel, without sensitive details.
Do not send raw game screenshots, databases, backups, complete logs or tokens.

For ordinary bugs use the bug template: version, Windows version, resolution/scale,
short reproduction and manually redacted necessary text. Automated checks cannot
prove all attachments are safe; review before posting.

Game frames and SQLite data remain local. Optional public-announcement fetching
does not upload them. The tool never requests game-account credentials. Fixes must
preserve passive/local behavior and uncertainty handling.

Maintenance checks cover tracked files, full-history secrets, dependency advisories,
CI permissions and release contents. Library/model notices and sources are preserved.
Do not publish old private Git history or Actions logs/artifacts without auditing them;
changing repository visibility exposes Actions history as well.
