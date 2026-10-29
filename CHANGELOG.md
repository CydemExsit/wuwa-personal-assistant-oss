# Changelog

## 1.1.10 — 配裝提醒與精簡推薦理由（尚未發布）

- 聲骸總覽、低分單件及詳情直接顯示主詞條不符，並列出目前主詞條與可確認的建議方向。
  副詞條高分不掩蓋配裝問題；未知資料顯示待確認，自訂判定不冒充攻略推薦。
- 無音區推薦理由依場域分組，只列角色、定位、第幾件、原因。40／70與65／70
  分別顯示差30分與差5分，配裝缺口直接標明主詞條／套裝問題。
- 優先級、倍率及完整計算保留在第二層細節；同角色同位置的替代套裝合併顯示。
- 原有分數、需求點、場域排序、庫存優先設定與裝備紀錄不變。


## 1.1.9 — 無音區待培養庫存（尚未發布）

- 一鍵記錄需要的聲骸是否用完，有可培養庫存的無音區往後排。
- 狀態立即保存並更新首頁，支援整批調整，日期與重開不重設。
- 詳見[版本說明](docs/release-1.1.9.md)。


## 1.1.8 — 戰鬥隊伍與聲骸配裝（尚未發布）

- 各角色獨立多隊與主隊設定，共用聲骸，所有隊伍充能均須達標。
- 戰鬥雙暴、完整換裝比較、剩餘副詞條機率與其他位置的真實庫存補強。
- 保留品質評分；詳細條件與來源收在展開說明。
- 詳見[版本說明](docs/release-1.1.8.md)及[戰鬥配裝](docs/combat-build.md)。

## 1.1.7 — 已裝備聲骸自動同步（尚未發布）

- 高信心即時辨識到角色與明確位置後，自動更新先前手動保存的裝備。
- 強化後重掃、換裝與跨角色移動皆可同步；舊聲骸與歷史保留，不確定與歷史修復仍保護手動設定。
- 更新鎖定環境至 Certifi 2026.10.19、Pillow 13.0.0，並同步 Certifi 對應來源。
- 詳見[版本說明](docs/release-1.1.7.md)。

## 1.1.6 — 角色面板版面（尚未發布）

- 摘要與數值依寬度並排，六項數值自動換行，來源與核對操作集中呈現。
- 收合技能操作列及節點間距，五件聲骸上移；保留字體大小與未儲存編輯。
- 詳見[版本說明](docs/release-1.1.6.md)。

## 1.1.5 — 角色常態數值（尚未發布）

- 新增屬性計算、來源明細、突破與具名節點，以及綁定配置的實測差異。
- 新增五格聲骸核對與差異明細、同件詞條即時更新，以及已查核的共鳴鏈效果。
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
