> 此獨立版本使用 `%LOCALAPPDATA%\WuwaDisplayAssistant`，與原版資料隔離。參閱 [雙模式版](display-edition.md)。

# Windows 開發與封裝

本專案以 Windows 10、64 位元 Python 3.12 與 one-folder PyInstaller 輸出為 V1 基準。封裝流程只使用安裝在本機虛擬環境內的套件與 RapidOCR 模型，不會在建置或首次啟動時下載 OCR 模型。

## 開發環境

從一般 PowerShell 執行，不需要系統管理員權限：

```powershell
cd wuwa-personal-assistant-oss
.\scripts\setup.ps1
.\scripts\run.ps1
```

`setup.ps1` 明確使用 `py.exe -3.12` 建立 `.venv`，並以 editable mode 安裝應用程式、測試及封裝相依套件。若 Python Launcher 找不到 Python 3.12，腳本會停止並顯示錯誤，不會退回其他 Python 版本。

若只想驗證啟動與全新本機資料庫，可使用隔離資料目錄：

```powershell
.\scripts\run.ps1 -SmokeTest -DataRoot "$env:TEMP\WuwaAssistantManualSmoke"
```

正式執行未指定 `-DataRoot` 時，使用者資料仍放在 `%LOCALAPPDATA%\WuwaDisplayAssistant\`，不會寫入 repository。

## 完整品質檢查

```powershell
.\scripts\test.ps1
```

此腳本依序執行 pytest、Ruff lint、Ruff format check、mypy、`pip check`、永久安全邊界掃描及 `git diff --check`。Qt 測試固定使用 offscreen platform。腳本不會格式化或修改原始碼。

## 建立 Windows one-folder 版本

```powershell
.\scripts\build.ps1
```

預設會先執行完整品質檢查，再使用 `packaging/WuwaDisplayAssistant.spec` 建立：

```text
dist/WuwaDisplayAssistant/
  WuwaDisplayAssistant.exe
  _internal/
```

若同一次工作流程已經完成完整檢查，可明確使用 `-SkipQualityChecks`，但正式交付前仍應至少執行一次 `scripts/test.ps1`。

封裝規格與 build 腳本會驗證下列資產：

- RapidOCR wheel 內既有的三個 ONNX 模型與 YAML 設定。
- Qt Windows platform plugin `qwindows.dll`；PySide6 的標準 PyInstaller hook 負責收集 Qt runtime 與 plugins。
- Alembic migration Python 檔案。
- 版本化 Roll、評分模板與強化決策政策 JSON。
- 應用程式深色主題 QSS。
- 八名角色的本機頭像與來源／雜湊 manifest；未知角色使用文字頭像，不會在執行時下載。
- `tzdata` 的 IANA 時區資料。

缺少任一 OCR 模型時，建置會立即失敗並列出缺少的本機路徑。流程沒有模型下載 fallback。輸出完成後，腳本以隱藏、offscreen 模式執行 `WuwaDisplayAssistant.exe --smoke-test`，資料寫到唯一的暫存目錄，驗證後才安全刪除該目錄。

建置腳本會暫時將 PyInstaller 的 `PATH` 限制為本專案 Python 環境、其基底 Python 與
Windows 系統目錄，完成或失敗後都還原原本設定。這可避免其他工具的同名 DLL 被誤收進
封裝。例如 Poppler 的 `icuuc.dll` 缺少 Qt 使用的 Windows ICU 函式，會造成
`DLL load failed while importing QtCore`。不需修改全機 PATH 或另行下載 DLL；請透過
`scripts/build.ps1` 重建完整目錄。封裝的 OCR 自我檢查及啟動測試會在還原 PATH 後執行。

## 發佈與已知限制

- V1 是可直接複製的 one-folder 版本，不是單一 EXE；`_internal` 不可移除。
- 目前沒有程式碼簽章。Windows SmartScreen 可能對未簽章的私人應用程式顯示提示。
- 不要只複製 EXE；應壓縮並傳遞整個 `dist/WuwaDisplayAssistant` 目錄。
- 應在乾淨的 Windows 10 非開發機器上再做一次啟動、資料保存及被動顯示器 capture 驗收。
- 後續 installer 建議使用 Inno Setup 或 WiX，安裝範圍採 per-user，資料仍保留在 `%LOCALAPPDATA%`。installer 不應內嵌管理員權限、自動啟動遊戲、輸入模擬或附掛遊戲的功能。

## 使用中的舊版與新版分開輸出

舊版仍開著時，可用 `scripts/build.ps1 -OutputDirectory dist/releases/1.1.0` 輸出到獨立資料夾。
輸出路徑限制在 repository 的 `dist` 下，完整執行相同的資產、OCR 和啟動驗證。
此雙模式版使用獨立資料根目錄，不會自動讀寫原版資料。要沿用原版紀錄，先從原版匯出備份，再到此版本預覽並匯入。此獨立版本後續更新則沿用 `WuwaDisplayAssistant` 內的資料。
