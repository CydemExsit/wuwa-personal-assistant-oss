> 雙模式版補充：本文件保留原版設計歷程。此獨立版本已授權本工具自己的被動小面板、點擊穿透、1080p/1440p ROI 對應；具體邊界與驗收以 [display-edition.md](display-edition.md) 為準。遊戲附掛、注入、控制與完整畫面持久化仍禁止。

# 被動即時畫面擷取設計與 Milestone 3 實作

> 狀態：Milestone 3 核心已實作並通過 fake capture 與安全測試；Milestone 4 才接上可見的
> 聲骸工作區與資料庫歷史。本文件不授權任何遊戲控制、程序互動或完整畫面保存功能。

目前實作對照：

- `capture.CapturedFrame` 是 context-managed lease，所有路徑由 `RoiExtractor` 的
  `finally` 立即釋放，並有 metadata-only `FrameLeaseAudit`。
- `FakeCaptureBackend`、`ImageSequenceBackend` 與 `MssMonitorCaptureBackend` 共用同一
  monitor-only contract；MSS 拒絕 virtual desktop index 0，只呼叫 `grab()`，沒有保存或
  錄影 API。
- `RoiPacket` 嚴格驗證 allowlist 與尺寸，保存 owned、read-only crop，不持有 frame view。
- `LiveRecognitionCoordinator` 採 0.5–0.75 秒時間門、generation token、active 1 + pending 1
  latest-wins worker，結果 callback 只含 JSON-safe DTO。
- `PageClassifier.classify_rois`、`EchoPageParser.parse_rois` 與 `LiveEchoRoiAnalyzer` 形成
  ROI-only OCR 路徑；正常 live analyzer 沒有 image debug writer。
- `LiveCaptureRuntime` 在獨立 capture thread 內擷取、裁切、釋放與排程；UI 不接觸 ndarray。
- `python -m wuwa_assistant.safety_scan` 與 Windows CI 會阻止禁用 dependency、API、錄影、
  Overlay／window attach 及 capture/full-frame write 路徑。

## 目標與不可逾越的邊界

即時模式只被動擷取使用者指定的**整個顯示器**，辨識聲骸頁面並把穩定狀態轉成既有的 `EchoObservation`、`EchoSession` 與 `EchoEvent`。結果顯示在第二螢幕上的一般獨立視窗；它不是 Overlay，不貼附遊戲視窗，也不覆蓋或攔截遊戲畫面。

任何實作都必須同時滿足以下限制：

- 不取得或依賴遊戲視窗、程序 ID 或程序 Handle；不讀取記憶體、封包或遊戲檔案。
- 不注入 DLL、不掛接繪圖管線、不使用 Windows UI Automation。
- 不產生或模擬滑鼠、鍵盤或手把輸入；依賴中不得包含 `pyautogui`、`pynput`、AutoHotkey、SendInput 包裝器或巨集套件。
- 不自動執行選擇、強化、調諧、鎖定、棄置或任何戰鬥行為。
- 不錄影、不建立完整畫面的螢幕截圖、不把完整畫面寫入暫存檔、log、crash dump 或資料庫。
- 不使用雲端 OCR、遙測或外部分析服務；畫面與 ROI 均不得離開本機。

若某個候選 backend、GUI 套件或診斷工具無法證明符合上述條件，就不得納入。

## 元件邊界

即時流程沿用第一階段既有的解析器與狀態追蹤器，新增部分必須以小介面隔離：

| 介面／元件 | 單一責任 | 安全要求 |
| --- | --- | --- |
| `CaptureBackend` | 依 `monitor_index` 取得一張 `CapturedFrame` | 只能擷取整個顯示器；不得接受視窗 Handle 或遊戲程序資訊 |
| `RoiExtractor` | 依解析度與頁面類型複製必要 ROI | ROI 必須是獨立的小型陣列，不可只是引用完整 frame 的 view |
| `StabilityGate` | 對具語意的固定 ROI 計算 pHash／SSIM 並判斷穩定 | 不雜湊角色模型、粒子背景等高動態區域 |
| `OcrScheduler` | 只排程最新一個已穩定且有變化的 ROI 集合 | 同時最多一個 OCR 工作；佇列不得保存完整 frame |
| `ObservationPipeline` | 分類、解析、信心門檻與事件去重 | 欄位無法確認時保留 `null`，不得由歷史值猜補 |
| `JsonStore` | 保存觀察、Session、事件及設定 | 僅結構化 JSON；禁止畫面位元資料及遊戲「特徵碼」作為 ID |
| `ResultWindow` | 在指定的第二螢幕呈現 JSON 衍生結果 | 一般獨立視窗；不使用透明／點擊穿透、不置入遊戲、不建立 Overlay |

`mss` 只作為可能的 `CaptureBackend` 候選。導入前仍須檢查版本、相依套件與實際呼叫路徑；核心流程不得依賴 `mss`，以便替換成另一個同樣只做桌面擷取的 backend。backend 的公開輸入只能是顯示器索引與擷取設定，不得悄悄尋找遊戲視窗。

## 單幀生命週期

1. 排程器以預設 **5 Hz**（每 200 ms；低效能裝置可設 4 Hz）呼叫一次 backend。
2. backend 在記憶體中建立完整顯示器 frame，附上單調時鐘與 wall-clock timestamp。
3. 先複製頁面分類 ROI；分類可信時，再複製該頁面的語意 ROI（名稱、等級、主屬性、副詞條與素材狀態）。解析度或縮放不符時回報 `unknown`，不得套用錯位 ROI。
4. ROI 複製完成後立即解除完整 frame 的所有引用；後續工作只接收 ROI。禁止把完整 frame 放入 callback、例外內容、重試佇列或除錯物件。
5. 對語意 ROI 建立 fingerprint 並送入穩定閘門。頁面類型改變時取消舊頁面的待處理工作並重設候選狀態。
6. 只有穩定且不同於上一個已處理狀態時才 OCR；OCR 產生觀察後再由 tracker 去重並輸出事件。
7. 保存 JSON，更新第二螢幕的獨立結果視窗；一般成功路徑不保存任何影像。

實作時應用 `try/finally` 確保例外或取消亦釋放 frame，並停用會包含陣列內容的除錯輸出。Python 無法保證立即清零所有底層緩衝區，因此安全承諾是「最短必要生命週期、無持久化、無跨元件保留」，而不是不實際的記憶體零殘留保證。

## 穩定判定與 OCR 排程

穩定判定只看固定 UI 欄位，不看星空背景、角色／聲骸模型或動畫。建議每個頁面使用兩層 fingerprint：

- `page fingerprint`：標題與頁面特有控制項，用於偵測頁面切換。
- `content fingerprint`：名稱、等級、兩列主屬性、五個副詞條及素材選取摘要，用於決定是否需要重新解析。

預設條件為至少 3 個相近樣本，且相近狀態持續 **500–750 ms**；5 Hz 下通常需要 4 個樣本才能跨越約 600 ms。pHash Hamming distance 與 SSIM 門檻必須以本機截圖及動畫切換樣本校準，不把暫定數值當成跨版本常數。頁面切換、ROI 大幅變動或 SSIM 驟降都會重新起算穩定窗。

OCR 排程規則：

- 新的穩定 `page fingerprint` 才重新分類頁面。
- 強化頁只有名稱／等級／主屬性／副詞條 ROI 之一改變後才重新解析；經驗條動畫本身不應觸發全文 OCR。
- 與上一個已處理 fingerprint 相同的穩定狀態不重複 OCR，也不重複發事件。
- 只保留「目前執行中」與「最新待處理」各一份小型 ROI；更新較新的狀態時取消或淘汰舊待辦（latest-wins），避免延遲結果倒序覆蓋新狀態。
- OCR 完成前若頁面 generation 已改變，丟棄該結果；tracker 只接收與目前 generation 相符的穩定觀察。
- OCR 信心不足時輸出 `null`，並等待下一次真正的 ROI 變化；不可反覆 OCR 同一幀來製造虛假的高信心。
- 停止指令採合作式 cancellation：先停止新擷取，再取消待辦、等待執行中的 OCR 安全結束、釋放 ROI，最後 flush JSON；停止流程本身不得擷取額外畫面。

4–5 Hz 足以在約 0.6–1.0 秒內確認穩定 UI，又不必以遊戲幀率擷取。除非測試證明頁面切換被漏掉，否則不提高頻率；調高只會增加 CPU、記憶體頻寬與敏感畫面在記憶體中的出現次數。

## 素材選取、取消與 Session 語意

即時模式不能把「出現在素材格」誤當成已完成消耗，因為使用者仍可能手動取消。素材頁的狀態採兩階段提交：

1. 偵測到新目標與已選來源時，立即為目標建立新的本機 Session ID（例如 `echo-session-0002`）；同名、同 COST、同主詞條也不得沿用來源 ID。
2. 來源 Session 只進入 `pending_consumption`，並記錄暫時的 `source -> target` 關係；此時 `was_consumed` 仍為 `false`，也不輸出最終的 `echo_used_as_material` 或 `echo_session_ended`。
3. 只有後續穩定畫面同時支持「目標已完成預測升級」且與 pending target 相容時，才原子式提交：將來源標為 `consumed_ended`、設定 `consumed_into_session_id`，依序輸出 `echo_used_as_material` 與 `echo_session_ended`。新目標繼續使用剛建立的 ID。
4. 若使用者返回未套用狀態、移除素材、關閉流程，或畫面證據互相矛盾，便取消 pending 關係；來源回復先前的 `active`／`discarded_active`，不得輸出已消耗事件。目標 Session 可繼續代表實際存在的新聲骸，但不能帶入預測等級。
5. 若程式結束、OCR 信心不足或證據逾時，狀態保持「未確認」；重啟後不得僅憑舊 pending 紀錄推定已消耗。之後只有新的穩定證據才能提交。

第一階段的九張固定序列有人工提供的真值，可把第 8→9 張視為消耗完成；未來即時模式沒有這項外部真值，必須遵守上述確認程序。

## 持久化與低信心除錯

正常路徑只保存：

- `EchoObservation`、`EchoSession`、`EchoEvent` 的 JSON；
- 欄位信心、原始 OCR 文字、ROI 名稱與 fingerprint；
- 擷取時間、顯示器索引、應用程式版本與 ROI 配置版本。

即時來源的 `source_file` 應為 `null`；不得捏造完整畫面的檔名或路徑。畫面右下角「特徵碼」不讀取、不保存，也不參與 Session ID。

只有欄位低於設定信心門檻時，才允許在 `debug_output/` 保存**該欄位的小尺寸 ROI**及其前處理版本。檔名使用本機流水號或隨機 ID，不含角色名或 OCR 內容；metadata 記錄門檻與欄位，不含完整 frame。除錯保存可完全關閉，並應提供數量／容量上限與定期清理策略。任何時候都禁止用「方便除錯」作為保存完整畫面的理由。

## 第二螢幕結果視窗

結果視窗只訂閱已保存或記憶體中的結構化 observation/event，不直接接觸 capture backend。它是可移動、可最小化的一般桌面視窗，預設放在使用者指定的第二螢幕；不使用透明或點擊穿透、不鎖定在遊戲上方、不讀取遊戲視窗位置，也不提供可觸發遊戲操作的按鈕。視窗關閉只停止呈現；停止擷取則透過本工具自己的明確控制完成。

## 威脅與安全檢查

| 風險 | 防護與驗證 |
| --- | --- |
| 套件間接提供輸入模擬 | 對直接與遞迴相依套件做 deny-list／人工審查；CI 檢查禁用套件與關鍵 API 名稱 |
| 完整 frame 因例外、佇列或診斷而落碟 | backend 後立即複製 ROI；有界 ROI 佇列；禁止 pickle／numpy dump／螢幕截圖 log；例外測試確認沒有完整圖片檔 |
| OCR 或遙測把畫面送出 | OCR backend 僅允許離線實作；應用程式不需要網路；不得加入 crash upload、analytics 或遠端字型／模型下載 |
| 動畫造成錯誤事件 | 語意 ROI、時間加連續樣本穩定窗、generation cancellation、tracker 再去重 |
| 素材尚未套用卻被判為已消耗 | `pending_consumption` 兩階段提交及取消／未確認路徑 |
| 結果視窗演變成 Overlay | GUI 與擷取解耦；禁止透明穿透、遊戲視窗附掛、繪圖 hook 與自動定位 |
| 顯示器或解析度設定錯誤 | 啟動時列出一般顯示器資訊供使用者選擇；解析度不符即停止解析並回報，不尋找遊戲程序補救 |

發布即時模式前須以 fake capture backend 完成可重複測試：完整 frame 不持久化、ROI 複製後可釋放來源、穩定窗與重複抑制、OCR latest-wins 取消、低信心才保存 ROI、素材提交與取消、以及所有禁用 API／相依套件檢查。真實 `mss` backend 則只做人工本機驗證，且仍不得新增完整畫面 fixture。
