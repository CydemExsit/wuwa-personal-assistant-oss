# 聲骸工作流程改善：驗收記錄（OSS，未發布）

本次不更改版本號，不建立 Release。此文件記錄本機驗收；對應提交的雲端品質、
安全與 Windows 封裝結果以 GitHub Actions 紀錄及交付回報為準。

## 功能與自動化驗收

| 驗收 | 結果與範圍 |
| --- | --- |
| A 第一層貢獻點 | PASS。既有 FarmBenefit.points 按角色／位置合計；60／70／80門檻、替代套裝雙份額、模型基礎點一致且原模型物件不變。第一層第五欄可讀，第二層按鈕實際開啟。 |
| B 共用庫存 | PASS。聲骸、聲骸強化及原規劃列內／整批共用單一服務和序列寫入；跨頁讀取、規劃／首頁通知、整批、SQLite重開、dataset隔離、儲存失敗與重複點擊。已提交但讀回失敗時停用舊值，重新載入恢復。 |
| C 離頁停止 | PASS。側邊欄／返回／程式導覽 × 手動／連續 × 已啟動／建立中共12組；重選同頁不停止，快速切頁不重啟，舊結果被隔離，小面板隱藏，已確認歷史仍在，Capture Session關閉。 |
| C 實際工作取消 | PASS。真實 LiveCaptureRuntime + 合成 backend／阻塞 analyzer：停止要求不等待原生呼叫，停止新增擷取、清除待處理OCR，釋放所有frame lease；原生呼叫安全完成後無結果回寫。 |
| D 品質／戰鬥分離 | PASS。+0、4、5、10、15、20、24、25 × 同件／候選16組；戰鬥未知保留可用品質決策。完整工作區與panel snapshot同值。既有戰鬥完整／不足、補強與機率測試通過。 |
| D 保守限制 | PASS。OCR待覆核、等級不明、COST不符、套裝不符、主詞條缺漏、副詞條缺漏不能繞過資料與配裝檢查；完整既有測試涵蓋拆套與未知比較等情境。 |

先跑相關既有基準測試：兩版各117例均通過。新加40個GUI案例與1個runtime案例，
加上調整後既有理由／runtime測試，共51個聚焦案例在各版獨立通過。

## 完整品質檢查

- `scripts/test.ps1` PASS：1696例收集、1695通過、1個既有選用網路測試略過。
- Ruff lint、Ruff format、mypy（207個來源檔）、pip check、安全掃描及git diff --check均PASS。
- 相依套件漏洞稽核PASS：零已知漏洞。私人本機環境的舊urllib3升至2.8.0後重掃，公開資料回歸通過；OSS沿用既有含2.8.0的hash鎖定環境。
- OSS另做公開檔案及完整Git歷史的遮蔽憑證掃描，均PASS。
- 只有既有SQLite日期adapter棄用警告，無測試失敗。沒有改動評分、順位公式或資料庫schema。

## Windows原生Qt版面

`windows`平台plugin、實測DPR1.25及2.0，使用隔離合成資料輸出並目視檢查視窗渲染。
完整／小型工作區、推薦理由1000與620邏輯像素、庫存860與620邏輯像素，以及小面板皆已檢查。
620寬推薦表原因欄281、貢獻欄70；庫存套裝252、狀態161，兩個管理視窗均無水平溢出。
聲骸強化庫存按鈕放在標題旁，小視窗也可直接找到。完整工作區沿用既有外層捲動保底。

Windows畫面擷取helper在恢復重試後仍逾時；以上為原生Qt自有視窗渲染與量測，
不是成功的外部視窗擷取／滑鼠自動化聲明。未啟動實際顯示器擷取，不接觸玩家資料。

## 兩版差異與實機限制

四項功能與回歸測試相同；保留私人版1440p限制、既有資料保護／首次設定／提醒相容，
OSS保留1080p+1440p支援、文字肖像、公開素材與hash依賴／安全／封裝政策。
僅對各版既有片段套用經檢查的修改，未覆蓋整份差異檔案或同步私人資料。

需實機另確認：遊戲執行中的CPU／GPU釋放體感、不同實體螢幕／字型／縮放組合、
真實OCR模型在進行中安全結束的耗時。已執行中的原生呼叫合作式結束，不強制殺掉執行緒；
本機合成驗收不宣稱整隊DPS準確度或遊戲內實測。

## 此版修改檔案

- `CHANGELOG.md`
- `README.md`
- `docs/README.md`
- `docs/combat-build.md`
- `docs/display-edition.md`
- `docs/echo-workflow-improvements.md`
- `docs/echo-workflow-verification.md`
- `src/wuwa_assistant/app/main.py`
- `src/wuwa_assistant/application/replacement_training.py`
- `src/wuwa_assistant/application/tacet_stock.py`
- `src/wuwa_assistant/live/runtime.py`
- `src/wuwa_assistant/ui/controllers/echo_workspace.py`
- `src/wuwa_assistant/ui/controllers/live_capture.py`
- `src/wuwa_assistant/ui/controllers/scoring_profiles.py`
- `src/wuwa_assistant/ui/controllers/tacet_stock.py`
- `src/wuwa_assistant/ui/controllers/today_plan.py`
- `src/wuwa_assistant/ui/dialogs/tacet_stock.py`
- `src/wuwa_assistant/ui/main_window.py`
- `src/wuwa_assistant/ui/pages/echo_live.py`
- `src/wuwa_assistant/ui/pages/today_plan.py`
- `src/wuwa_assistant/ui/widgets/equipment_workspace.py`
- `src/wuwa_assistant/ui/widgets/farming_reasons.py`
- `tests/gui/test_echo_fit_reasons.py`
- `tests/gui/test_echo_workflow.py`
- `tests/live/test_runtime.py`
