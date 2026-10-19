# 鳴潮本機助手 / Wuwa Personal Assistant

在 Windows 本機辨識聲骸、比較配裝分數，整理角色養成與待辦的《鳴潮》非官方社群工具。
離線 OCR 與評分在本機完成；單螢幕可用置頂小面板，雙螢幕可用完整工作區。

[![CI](https://github.com/CydemExsit/wuwa-personal-assistant-oss/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CydemExsit/wuwa-personal-assistant-oss/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[下載 Windows 版](https://github.com/CydemExsit/wuwa-personal-assistant-oss/releases/latest)**
· [快速開始](#快速開始)
· [使用文件](docs/README.md)
· [問題回報](https://github.com/CydemExsit/wuwa-personal-assistant-oss/issues/new/choose)
· [English](#english-overview)

本專案與 KURO GAMES 無隸屬、授權、贊助或合作關係。不操作遊戲、不讀取遊戲程序或記憶體。

## 可以做什麼

| 功能 | 使用方式 |
| --- | --- |
| 聲骸辨識 | 手動啟動後，被動辨識指定顯示器上支援的聲骸頁面；資料不足時提示人工確認。 |
| 評分與比較 | 查看聲骸分數、替換目標分數、保存與升級建議，以及可檢視的評分依據。 |
| 單／雙螢幕模式 | 單螢幕顯示工具自己的置頂小面板；雙螢幕使用完整工作區。 |
| 養成管理 | 整理角色、裝備位置、素材、待辦、抽卡規劃與歷史紀錄。 |
| 角色數值（校準中） | 查看非戰鬥常態面板與來源、突破階段及具名節點；[範圍與已知限制](docs/character-panel-stats.md)。 |
| 本機資料 | 保存設定、匯出備份與還原；辨識和評分不依賴雲端。 |

![單螢幕小面板：聲骸分數、替換目標與保存／升級建議的合成示意](docs/images/echo-panel-demo.png)

*圖中使用虛構資料，只示範介面；不是玩家截圖或實際評分結果。公開版角色使用文字縮寫，不附遊戲圖片。*

## 系統需求

| 項目 | 支援範圍 |
| --- | --- |
| 作業系統 | Windows 10 2004+／Windows 11，x64 |
| 遊戲語言 | 繁體中文 |
| 遊戲顯示 | 無邊框，填滿所選顯示器 |
| 遊戲解析度 | 1920×1080 或 2560×1440，須與所選顯示器的實際像素尺寸一致 |
| 螢幕配置 | 單螢幕小面板或雙螢幕完整工作區，可在設定切換 |

其他比例、黑邊、非滿螢幕視窗與獨佔全螢幕不在目前支援範圍。
OCR 可能受字型、縮放、動畫及遊戲 UI 更新影響；1080p 的 ROI／runtime 與縮放樣本已驗證，仍需不同原生環境回饋。

## 下載與安裝

到 **[最新正式版 Releases](https://github.com/CydemExsit/wuwa-personal-assistant-oss/releases/latest)**，
在 Assets 選擇檔名結尾為 **`-Setup.exe`** 的安裝檔。安裝不需要 Python、Git、PowerShell 指令或管理員權限；OCR 模型已包含，首次啟動不另行下載。

| 檔案 | 適用情境 |
| --- | --- |
| `WuwaPersonalAssistant-OSS-版本-Setup.exe` | 一般使用者：安裝後從桌面或開始功能表開啟「鳴潮本機助手 OSS」。 |
| `WuwaPersonalAssistant-OSS-版本-windows-x64.zip` | 免安裝：完整解壓，保留 EXE 同層的 `_internal`，執行 `WuwaDisplayAssistant.exe`。 |
| `SHA256SUMS.txt` | 核對同一版下載檔案的 SHA-256。 |
| `ThirdPartySources`、`certifi`、`VerificationReports`、`Source code` | 第三方來源、驗證報告與原始碼；一般安裝不需要下載。 |

封裝目前未簽章，Windows 可能顯示 SmartScreen 提示。請確認來源為本專案正式 Release，
並核對同版雜湊；不要停用防毒或 Windows 安全功能。

## 快速開始

1. 安裝並開啟工具，在單頁「快速開始」確認模式、遊戲螢幕與解析度。
2. 按「測試這個螢幕」，通過後按「進入工具」。遊戲須使用繁中、無邊框並填滿所選螢幕。
3. 開啟遊戲的聲骸選擇清單，在工具「聲骸強化」選好角色與評分設定，按「啟動小面板」或「開始辨識」。
4. 查看結果；辨識不足時回工具按「修正 OCR」，使用完畢按「結束小面板」或停止辨識。

角色與評分模板可稍後調整。模式、螢幕與解析度可在設定修改；擷取只會在你明確按下測試或開始後執行。

單螢幕面板按住 **Ctrl+Shift** 再以滑鼠拖曳，放開後恢復點擊穿透。
離開聲骸頁時面板仍會顯示，保留的結果標為「上次結果」；未選替換目標或資料不足時顯示未知，不猜成零。
詳見[單／雙螢幕使用說明](docs/display-edition.md)。

## 更新與資料保留

工具開啟時會檢查正式新版，僅在工具內提示；也可到「設定 → 一般設定 → 程式更新」按「檢查新版」。
按「下載新版」後會驗證正式 Release 的 SHA-256，再由你按「安裝並重新開啟」。
啟動檢查可關閉；不會自動下載或安裝，工具關閉後也不會執行。

更新前先儲存編輯中的表單。工具會停止辨識、完成已接受的背景保存並關閉資料庫，再安裝及重新開啟；安裝期間請勿關機。
安裝版沿用原位置；ZIP 版使用工具內更新會轉為目前使用者的安裝版，資料位置不變。
1.1.4 以前的版本需先手動安裝 1.1.4 或更新版本，之後才能使用此更新入口。

角色、聲骸、歷史、設定與備份預設位於 **`%LOCALAPPDATA%\WuwaDisplayAssistant`**。
更新與 Windows「已安裝的應用程式」解除安裝均保留此資料。
若電腦另有共用此資料位置的 Display 版，開啟 OSS 前請先關閉另一版。
詳見[程式更新](docs/updates.md)與[備份還原](docs/data-management.md)。

## 常見問題

| 狀況 | 處理方式 |
| --- | --- |
| 黑畫面／未看到支援的聲骸頁 | 停止辨識，確認遊戲語言、無邊框、實際解析度與所選顯示器，再執行擷取測試；工具不會自動改動 Windows 或遊戲設定。 |
| 文字辨識不完整／分數待確認 | 回工具「修正 OCR」補齊欄位；未知資料不會當成零，也不會強行給出明確建議。 |
| 面板停留在舊結果 | 查看是否標為「上次結果」，回支援的聲骸清單等待分析；要停止時回工具按「結束小面板」。 |
| 無法檢查或下載新版 | 離線仍可使用目前版本；也可從正式 Release 手動下載安裝檔。 |
| 想回報問題 | 使用[問題回報表單](https://github.com/CydemExsit/wuwa-personal-assistant-oss/issues/new/choose)，提供版本、顯示設定與重現步驟，勿附私人截圖或資料庫。 |

## 隱私、安全與使用限制

- **本機辨識**：完整畫面只短暫存在記憶體，立即裁出允許的辨識區域（ROI）；不保存完整即時畫面、不上傳畫面、不使用雲端 OCR 或遙測。
- **聯網範圍**：公告／兌換碼等公開資料與程式更新可聯網取得；不傳送玩家資料，辨識與評分不依賴雲端。
- **被動工具**：不模擬滑鼠／鍵盤／手把，不自動強化、選擇或棄置，不讀取遊戲檔案、程序、記憶體或封包。
- **獨立面板**：小面板是工具自己的 Qt 視窗，排除於自身擷取；不使用遊戲注入、程序附掛、DLL injection 或 graphics hook。
- **建議的範圍**：分數是養成比較模型，不是官方評分、傷害模擬或帳號安全保證；OCR 不確定時保留人工確認。

安全／隱私問題請依 [SECURITY.md](SECURITY.md) 回報，不要把敏感內容貼到公開 Issue。

## 文件與開發

- **使用者**：[文件索引](docs/README.md)、[雙模式操作](docs/display-edition.md)、[資料與備份](docs/data-management.md)、[版本更新記錄](CHANGELOG.md)。
- **評分與辨識**：[評分方法](docs/scoring-methodology.md)、[角色評分來源](docs/character-scoring-sources.md)、[OCR 設計](docs/ocr-strategy.md)、[GPU OCR 與測量範圍](docs/gpu-ocr-performance.md)。
- **貢獻者**：[開發與貢獻指南](CONTRIBUTING.md)、[Windows 封裝](docs/windows-packaging.md)、[第三方告知](THIRD_PARTY_NOTICES.md)、[依賴授權與對應來源](docs/dependency-licensing.md)。

從原始碼執行需要 Windows x64、Git、Python 3.12 與 Windows Python Launcher：

```powershell
git clone https://github.com/CydemExsit/wuwa-personal-assistant-oss.git
cd wuwa-personal-assistant-oss
.\scripts\setup.ps1
.\scripts\run.ps1
```

提交前執行 `.\scripts\test.ps1`。品質關卡包含完整測試、Ruff、格式、mypy、依賴與安全檢查；
CI 另檢查公開檔案、歷史憑證及 Windows 封裝／啟動／離線 OCR。
公開測試以合成資料為主，需私人校準圖的測試明確略過，私人資料不隨 repo 散布。
PowerShell 若阻擋腳本，依你管理的 Windows 環境允許受信任的本機腳本，不需要更改全機設定。

本專案仍屬早期公開專案，尚未宣稱外部使用者數、下載量或廣泛採用。
公開來源從清理後的 1.1.1 開始，不移植含私人作者資料與官方圖片的舊歷史。
歷史設計、版本與測量文件的適用範圍見[文件索引](docs/README.md)。

## 授權

自有程式碼、文件與簡單圖形圖示採 [MIT](LICENSE)。函式庫、模型、遊戲商標與第三方內容各有其權利，
不因本專案的 MIT 授權而改變；分享成品時請保留[第三方告知](THIRD_PARTY_NOTICES.md)。

## English overview

An unofficial local-first Wuthering Waves desktop assistant for Windows x64.
It provides offline Echo OCR and scoring, replacement comparisons, cultivation planning,
local history and backups, plus single-monitor panel and dual-monitor workspace modes.
The supported game UI is Traditional Chinese at 1920×1080 or 2560×1440 in a borderless
window filling the selected monitor. The app itself is primarily in Traditional Chinese.

Download the `-Setup.exe` from the [latest release](https://github.com/CydemExsit/wuwa-personal-assistant-oss/releases/latest).
Python is only needed for source development. OCR models are bundled.
The panel is a separate application window: no game injection, hooks, input automation,
memory access, telemetry or screenshot uploads. Updates require explicit download and installation.
Game artwork and private samples are excluded. This project is not affiliated with KURO GAMES.

See the [documentation index](docs/README.md), [contributing guide](CONTRIBUTING.md),
[security policy](SECURITY.md) and [MIT license](LICENSE). Reports in Chinese or English are welcome.
