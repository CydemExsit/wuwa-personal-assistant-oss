# 鳴潮本機助手 / Wuwa Personal Assistant

《鳴潮》**非官方社群工具**：在 Windows 本機辨識聲骸、比較分數與規劃養成。
使用 Python／PySide6、SQLite 與離線 OCR；不操作遊戲、不讀取遊戲程序或記憶體。
與 KURO GAMES 無隸屬、授權、贊助或合作關係。

[![CI](https://github.com/CydemExsit/wuwa-personal-assistant-oss/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CydemExsit/wuwa-personal-assistant-oss/actions/workflows/ci.yml)
[MIT](LICENSE) · [使用說明](docs/display-edition.md) · [問題回報](https://github.com/CydemExsit/wuwa-personal-assistant-oss/issues)

## 功能與支援

- 手動啟動後，被動辨識指定顯示器的支援聲骸頁面。
- 顯示聲骸分數、替換目標分數、保存／升級建議；低信心資料要求人工確認。
- 單螢幕使用工具自己的置頂小面板，雙螢幕使用完整工作區，可隨時切換。
- 管理角色養成、裝備位置、歷史、素材、待辦、抽卡規劃與本機備份。
- 角色使用文字縮寫；不散布遊戲圖片、活動圖片或私人 OCR 素材。

支援 **Windows 10 2004+／Windows 11 x64**，遊戲繁體中文、無邊框並填滿所選顯示器。
支援 1920×1080、2560×1440；不支援任意比例、黑邊、非滿螢幕或獨佔全螢幕。

![小面板合成資料示意](docs/images/echo-panel-demo.png)

圖中為虛構介面示意，不是玩家截圖或實際評分結果。

## 安裝與第一次使用

一般使用者到 [Releases](https://github.com/CydemExsit/wuwa-personal-assistant-oss/releases)
下載 Windows ZIP，**完整解壓**，保留 EXE 同層 `_internal`，執行 `WuwaDisplayAssistant.exe`。
首個 OSS 版本為 **1.1.1**。封裝目前未簽章；請核對同一 release 的 SHA-256。

從原始碼執行需要 Git、Python 3.12 與 Windows Python Launcher：

```powershell
git clone https://github.com/CydemExsit/wuwa-personal-assistant-oss.git
cd wuwa-personal-assistant-oss
.\scripts\setup.ps1
.\scripts\run.ps1
```

安裝會下載鎖定依賴並檢查雜湊。若 PowerShell 原則阻擋腳本，依你管理的 Windows
環境允許受信任的本機腳本即可，不需要更改全機設定。

首次啟動選單／雙螢幕、遊戲顯示器與解析度，完成擷取測試。單螢幕按「啟動小面板」，
回遊戲查看結果；按住 Ctrl+Shift 拖曳，放開後穿透點擊。回工具按「結束小面板」停止。
面板不因離開聲骸頁自動消失，舊結果會清楚標示；資料不足時回工具「修正 OCR」。
詳見 [雙模式說明](docs/display-edition.md)。

## 隱私、安全與限制

- 畫面只短暫存在記憶體，立即裁出允許的 ROI；不保存完整即時畫面、不上傳畫面、不使用雲端 OCR 或遙測。
- 角色、聲骸、歷史、設定與備份保存在本機。預設根目錄為 `%LOCALAPPDATA%\WuwaDisplayAssistant`。
- 公告／兌換碼等公開資料可聯網更新；辨識與評分不依賴雲端，不傳送玩家資料。
- 不模擬滑鼠／鍵盤／手把，不自動強化、選擇或棄置，不讀取遊戲檔案、記憶體或封包。
- 小面板是工具自己的 Qt 視窗並排除於自身擷取；禁止遊戲注入／附掛、DLL injection、graphics hook、程序附掛及戰鬥自動化。
- 分數是可檢視的養成比較模型，不是官方評分、傷害模擬或帳號安全保證；未知資料不猜成零。
- OCR 受字型、縮放、動畫及 UI 更新影響。1080p 已有 ROI／runtime 與縮放樣本驗證，仍需不同原生環境回饋。

本專案尚未宣稱外部使用者數、下載量或廣泛採用。早期開發在私人 repo 進行；此 repo
從清理後的 1.1.1 開始，不移植含私人作者資料及官方圖片的舊歷史。

## 開發與文件

```powershell
.\scripts\test.ps1
.\scripts\build.ps1 -SkipQualityChecks -OutputDirectory dist/releases/1.1.1
```

品質關卡包含完整測試、Ruff、格式、mypy、依賴與安全檢查。CI 另檢查可公開檔案、
歷史憑證與 Windows 封裝／啟動／離線 OCR。公開測試以合成資料為主；需要私人校準圖的
測試明確略過，私人資料不隨 repo 散布。

- [貢獻與可重現安裝](CONTRIBUTING.md)、[安全回報](SECURITY.md)、[變更記錄](CHANGELOG.md)
- [評分方法](docs/scoring-methodology.md)、[OCR 設計](docs/ocr-strategy.md)、[資料與備份](docs/data-management.md)
- [效能測量與範圍](docs/performance.md)、[歷史詳細說明](docs/user-guide.md)
- [第三方告知](THIRD_PARTY_NOTICES.md)、[依賴授權與對應來源](docs/dependency-licensing.md)

自有程式碼、文件與簡單圖形圖示採 [MIT](LICENSE)。函式庫、模型、遊戲商標及第三方內容
各有其權利，不因本專案的 MIT 授權而改變；分享成品時請保留第三方告知。

## English overview

An unofficial local-first Wuthering Waves desktop assistant for Windows x64,
with offline Echo OCR/scoring, replacement comparisons, planning, local history
and backups, and single/dual-monitor modes. Game UI support is Traditional Chinese
at1080p/1440p in a borderless full-monitor window. The passive panel is a separate
application window: no game injection, hooks, input automation, memory access,
telemetry or screenshot uploads. Game artwork and private samples are excluded.
See the PowerShell setup above, [contributing](CONTRIBUTING.md), [security](SECURITY.md)
and the [MIT license](LICENSE). This is an early public project; external adoption
has not been established.
