# Windows 安裝與開發封裝

[文件索引](README.md) · [下載與安裝](../README.md#下載與安裝)

一般使用者請使用 README 的 **Setup.exe 安裝檔**，不需要開發環境。
完整建置腳本在 one-folder 程式通過稽核、啟動與離線 OCR 後，另產出：

- `*-X.Y.Z-Setup.exe`：預設的一般安裝版，使用者目錄、桌面／開始捷徑、Windows 解除安裝。
- `*-X.Y.Z-windows-x64.zip`：免安裝替代方案，完整解壓並保留 `_internal`。
- `SHA256SUMS.txt`、`VerificationReports-X.Y.Z.zip`；OSS 另提供對應的第三方來源，來源包依其依賴版本命名，不必與應用版本相同。

安裝器採 Inno Setup，`PrivilegesRequired=lowest`，不要求管理員權限；程式安裝在
`%LOCALAPPDATA%\Programs`，與既有 `%LOCALAPPDATA%\WuwaDisplayAssistant` 資料分開。
兩版安裝識別、資料夾與捷徑各自獨立，應用資料位置沿用舊版。關閉工具後執行新版安裝檔即可更新。
解除安裝移除程式與捷徑，保留角色／歷史／設定／備份。不自動設定開機啟動。

建置使用已安裝的 Inno Setup 6 或 7（可設定 `ISCC_EXE`）；GitHub Windows CI 已提供。
這是開發／CI 依賴，一般使用者與首次啟動均不需下載工具或模型。
本機只驗證 one-folder 時可指定 `-SkipDistribution`；正式交付須由 CI 完成安裝檔驗證。
`scripts/verify-installer.ps1` 以隔離含中文空格路徑測試安裝、全檔雜湊、開始捷徑、
已安裝程式啟動／離線 OCR、重裝、解除安裝及資料保留。若發現已正式安裝同版，拒絕覆寫它做測試。
CI 保留安裝與封裝報告，以及可直接放進 Release 的安裝檔與免安裝 ZIP。

## 開發環境與品質關卡

```powershell
.\scripts\setup.ps1
.\scripts\test.ps1
.\scripts\build.ps1 -SkipQualityChecks -OutputDirectory dist/local-build
```

需要 Windows x64、Python 3.12 與 Python Launcher。`test.ps1` 包含 pytest、Ruff、格式、
mypy、依賴與安全檢查。正式交付必須完成完整品質關卡，`-SkipQualityChecks` 僅用於同次已檢查後的建置。
原始碼可用 `.\scripts\run.ps1` 執行。封裝會檢查本機 OCR 模型、Qt platform、資料與遷移，
暫時隔離建置 PATH 避免其他軟體的同名 DLL 混入；不修改全機 PATH。

## 首次使用與限制

快速開始只有一頁，自動建議模式／支援解析度與主要支援螢幕；多螢幕時仍需確認遊戲實際所在位置。
不以螢幕數量推測遊戲位置，也不擷取畫面直到明確按下測試。測試成功後才能完成設定；
黑畫面、不支援尺寸、缺模型與過時結果均有保護。模式可在設定修改，舊有設定無需重做。
Windows 10 2004+／11 x64；遊戲繁中、1080p 或 1440p 無邊框滿螢幕。
未簽章可能顯示 SmartScreen 提示；不要停用防毒或系統安全功能。
本機與 CI 隔離測試並不等於已在所有乾淨 Windows 實體機或實際遊戲配置驗證。
