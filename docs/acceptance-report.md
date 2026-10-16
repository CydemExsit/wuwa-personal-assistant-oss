# 驗證方法與範圍

私人開發階段曾以九張本機畫面驗證頁型、欄位與 Session 時間鏈；樣本數有限，
不能推論一般準確率或外部採用。原始畫面、逐張人工真值、raw OCR 與個人聲骸數值
不隨 OSS repo 散布。早期私人驗收不是陌生使用者可從此 repo 重跑的資料集。

公開驗證使用程式產生的合成影像／結構化資料測試 OCR 合約、ROI、穩定門、Session、
評分、人工確認、SQLite 與 GUI。需要私人校準圖片的測試會明確略過。
完整命令為 `scripts/test.ps1`；封裝另外驗證離線 OCR、隔離資料根目錄啟動、
來源／資源／原生元件／第三方告知及模型雜湊。參見 [貢獻](../CONTRIBUTING.md)。

保留以下設計與後續歷史驗證記錄，作為方法與限制的說明；不是公開採用的證明。

## 即時取樣與圖示模板建議

- 初始取樣率：4–5 Hz。
- 穩定條件：至少 3 個相近語意 ROI 樣本，並持續約 0.5–0.75 秒；5 Hz 實作通常需 4 個樣本跨越約 0.6 秒。
- OCR 只在 page/content fingerprint 穩定且不同於上一個已處理狀態時執行。
- 本批九張不需要額外圖示模板即可通過。
- 未來 `已棄置` toast 消失後，應加入棄置切換按鈕／卡片角標模板；這是目前最值得新增的模板。
- 若更多樣本出現 `7.1/7.5`、`8.6/9.4` 混淆，再加入固定解析度的 `0-9 . %` 小模板作最後回退；目前不需要第二套大型 OCR。

## 可重複測試

```text
python -B -m pytest -q -p no:cacheprovider
............................... [100%]
31 passed

python -B -m ruff check .
All checks passed!

python -B -m ruff format --check .
36 files already formatted

python -B -m mypy src
Success: no issues found in 25 source files

python -m pip check
No broken requirements found.
```

Private acceptance command：

```powershell
python -B -m wuwa_assistant analyze-sequence samples_private `
  --output debug_output\acceptance.json
```

結果：9 samples、0 differences、16 events、2 sessions。

## 第一階段當時的下一階段建議（V1 已完成第 2–5 項）

1. 補充不含錄影的「轉場／動畫中」單張截圖，以及 toast 已消失但棄置圖示仍存在的截圖，用來校準 StabilityGate 與圖示模板。
2. 先以 fake capture backend 完成完整 frame 不落碟、ROI 複製後釋放、latest-wins OCR 與停止流程測試。
3. 再加入可替換的 `mss` whole-monitor backend；預設 4–5 Hz，不能接收遊戲視窗或程序 Handle。
4. 實作素材 `pending_consumption` 的兩階段確認／取消流程，再做長時間被動監看測試。
5. 最後才考慮第二螢幕的一般獨立結果視窗；不採 Overlay，不加入任何操作遊戲的按鈕。

## Windows 桌面 V1 驗收（2026-08-08）

Milestone 2–9 已全部完成並保留分段 commit。V1 版本為 `1.0.0`，正常啟動提供繁中深色
PySide6 一般視窗與八頁導覽；Capture、OCR、SQLite 與 UI worker 分離，程式啟動時不會
持續擷取顯示器。

### 可用流程

- 首次啟動精靈：安全邊界、顯示器偵測、一次性被動 ROI 測試、資料路徑確認、第一角色
  或虛構 demo、第一個評分模板。
- 聲骸強化：手動開始／停止 monitor-only capture、穩定門、離線 OCR、Session timeline、
  人工修正 audit、角色設定檔評分、Roll breakdown 與規則／枚舉停止建議。
- 養成管理：角色、武器、技能、節點、三人隊伍、五件永久聲骸、breadth-first queue、
  素材缺口、波片規劃與估計完成日。
- 日常規劃：每日／每週／限時活動待辦、Asia/Taipei 到期分類、延後與 recurrence，以及
  全手動星聲、抽券、目標、收入與三情境缺口。
- 資料安全：一致性 SQLite backup、versioned JSON export、完整 import preview、重啟前
  staged restore、rollback、損毀拒絕與 allowlist 診斷包。
- Windows 交付：`scripts/setup.ps1`、`run.ps1`、`test.ps1`、`build.ps1`，以及本機
  one-folder `dist/WuwaPersonalAssistant/`。

### 自動化品質結果

正式 `scripts/test.ps1` 使用兩個獨立 Qt process，避免 Windows offscreen Qt runtime 在
數百個 widget teardown 後重用造成 native crash；兩段合計收集並執行 397 tests：

```text
tests/test_app_startup.py                         4 passed
其餘 pytest                                     393 passed
ruff check                                      passed
ruff format --check                             212 files formatted
mypy src                                        125 source files, passed
pip check                                       no broken requirements
wuwa_assistant.safety_scan                      passed
  direct dependency declarations               17
  Python source files                           124
git diff --check                                passed
```

pytest 只有 Python 3.12 `sqlite3` 預設 date／datetime adapter 的棄用警告；沒有測試失敗。
Restore、export/import、GUI presenter、fake capture 與 no-full-frame-write 路徑都有獨立
regression tests。Windows symlink 測試會在目前帳號不允許建立 symlink 時跳過，其餘
restore case 均通過。

### 封裝驗證

```text
PyInstaller                                     6.21.0
輸出                                            dist/WuwaPersonalAssistant/
WuwaPersonalAssistant.exe                      13.22 MiB
one-folder 總大小                              350.77 MiB / 1112 files
packaged --smoke-test                           exit 0
```

Build preflight 與 post-build 會驗證三個 RapidOCR ONNX 模型、`qwindows.dll`、Alembic
migration、scoring JSON、dark QSS 與 `tzdata/Asia/Taipei`。所有模型來自已安裝的本機
RapidOCR wheel；沒有下載 fallback。PyInstaller warnings 只涉及 Windows 不存在的 POSIX
模組與刻意排除／未安裝的非 ONNX OCR backend。

### 永久安全邊界驗證

- 安全掃描未發現輸入模擬、遊戲程序 Handle、記憶體讀取、注入、hook、Overlay、封包
  或雲端 OCR 相依套件／原始碼。
- MSS backend 只能列出並擷取使用者選取的實體 monitor；不接受 window 或 process。
- 完整 frame 只有短生命週期 lease，ROI copy 後立即 release；API 沒有 full-frame save
  或錄影方法，自動測試也確認正常 live 與 onboarding 測試不產生完整畫面檔。
- 診斷包 manifest 明確排除 database、full-frame、screenshots、debug images 與 ROI images。
- 正式資料、備份、log、exports、debug 與 build output 均在 repository 外或 Git ignore。

### V1 已知限制

- 發行版尚未程式碼簽章，也沒有正式 installer；必須保留整個 one-folder 目錄。
- 已在本機 Windows 10 完成 packaged smoke，仍建議在另一台乾淨、非開發 Windows 10
  電腦做一次顯示器權限、OCR 與資料重啟驗收。
- UI 只支援目前校準的 2560x1440 繁體中文遊戲版面；新 UI／新詞彙仍需使用者提供
  靜態截圖校準。工具不會因未知內容而猜測。
- 完整 Optimal Stopping、戰鬥模擬、網路公告／兌換碼同步與 Windows 通知皆為後續，
  且不會改變永久安全邊界。
