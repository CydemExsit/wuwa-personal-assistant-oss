# Changelog

## 1.1.5 — 角色常態數值（尚未發布）

- 新增屬性計算、來源明細、突破與具名節點，以及綁定配置的實測差異。
- 修正生命節點逐筆取整、副詞條最後乘法精度與攻擊比例路徑；本批配置已逐項吻合，其他等級與換裝須重新對照，詳見[版本說明](docs/release-1.1.5.md)。

## 1.1.4 — 工具內更新與資料保留 (2026-10-17)

- 在設定提供正式新版檢查、下載進度、取消及 SHA-256 驗證；下載與安裝均需使用者操作。
- 安裝前停止辨識、完成已接受的保存並安全關閉，更新後沿用資料位置重新開啟。
- ZIP 版經工具內更新轉為使用者安裝版；編輯中的表單需先自行儲存。
- 詳見[更新說明與驗證範圍](docs/release-1.1.4.md)、[程式更新](docs/updates.md)。

## 1.1.3 — 本機 GPU OCR (2026-10-17，未獨立發布，納入 1.1.4)

- Windows OCR 優先使用隨附 DirectML，裝置不可用時回退 CPU；不需 CUDA 或另下載模型。
- 限制推論佇列與執行資源，對非聲骸畫面採用有界探測間隔。
- 詳見[版本說明](docs/release-1.1.3.md)及[測量條件與限制](docs/gpu-ocr-performance.md)。

## 1.1.2 — Simple first installation (2026-10-16)

- Add a per-user Setup.exe with shortcuts, update/uninstall and portable ZIP.
- Reduce first-run setup to one page with topology-based mode and resolution defaults.
- Preserve explicit capture, uncertainty, existing data and third-party notices/sources.
- Verify installed contents, startup/OCR, shortcuts, reinstall, uninstall and data retention.
- Fence late runtime statuses during stop and publish cleanup completion atomically.

## 1.1.1 — OSS baseline (2026-10-16)

- Begin a clean repository from privately developed1.1.0. Old commits, personal
  author emails, screenshots and private sample truth files are not imported.
- Apply MIT to project-authored material and add an unofficial/nonaffiliation notice.
- Replace the game-art app icon with original SVG geometry; remove official
  portraits/event images, using initials and announcement links instead.
- Preserve offline OCR/scoring, single/dual modes, passive panel, manual confirmation,
  cultivation planning, local history and backups.
- Add hashed install profiles, public-file/history-secret checks, focused issue
  templates, contribution and security policies.
- Preserve library/model notices and corresponding Qt/PySide source; exclude
  unused virtual keyboard/QML/PDF/video payload from the Windows package.

Earlier [release notes](docs/release-1.1.0.md) describe private development and its
measurement scope. They do not demonstrate public adoption or public release history.
