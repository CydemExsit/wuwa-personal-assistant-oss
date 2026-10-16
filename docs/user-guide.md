> 歷史詳細說明：此文件保留私人開發階段的功能與版本記錄。公開版以 1.1.1 起步，不附官方圖片；角色使用文字縮寫，活動保留公告連結。歷史私人樣本結果不代表公開可重現資料或外部採用。現行入門請讀 [README](../README.md) 與 [雙模式說明](display-edition.md)。

# wuwa-personal-assistant-display

安全優先、離線優先的《鳴潮》Windows 本機養成工具。V1 已依
[`docs/product-plan.md`](product-plan.md) 完成可日常啟動的繁中 PySide6 桌面應用、
SQLite 資料層、被動顯示器辨識、聲骸評分、角色／素材／待辦／抽卡規劃、首次設定、
備份還原與 Windows one-folder 封裝。既有 2560x1440 繁體中文 OCR、ROI、穩定判定與
Session tracker 仍是獨立核心。

既有九張已標註 private samples 的驗收結果為：頁面分類 9/9、所有標註欄位零差異、16 個事件、2 個本機 Echo Session。詳見 [`docs/acceptance-report.md`](acceptance-report.md)。

目前獨立版本 **1.1.0** 提供單螢幕小面板與雙螢幕完整工具，可在設定隨時切換。以穩定版 1.0.35 為基底，使用獨立執行檔、設定與資料庫。詳見 [雙模式使用說明](display-edition.md) 與 [更新及驗收記錄](release-1.1.0.md)。

## 第一次使用

1. 快速開始選擇單／雙螢幕、遊戲顯示器及 1920x1080／2560x1440，完成擷取測試。
2. 讓無邊框遊戲填滿所選螢幕，進入聲骸清單，選取要辨識的聲骸。
3. 單螢幕模式在工具按「啟動小面板」，回到遊戲即可查看分數、替換目標與保存／升級建議。雙螢幕模式沿用完整工具的開始辨識與手動單次操作。
4. 面板持續顯示，按住 Ctrl+Shift 可拖曳；辨識不足時回工具「修正 OCR」，使用完畢回工具按「結束小面板」。

選項可在「設定 → 一般設定 → 被動擷取」修改並保存；兩種模式共用此版本內的資料。替換目標仍由角色聲骸位置選擇，未設定時顯示未知，不猜測分數。

畫面顯示「角色名裝配中」時，高信心辨識會自動登記所屬角色，新名字也會加入本機角色庫。
左側裝備位置與選中卡面能明確對應時，角色頁直接填入位置 1–5，不必按保留或再次挑選。
只確定角色時，聲骸會列在角色頁的「已登記裝備／位置待確認」。手動設定的位置優先保留；
也可選好角色與位置後按「保存至角色位置」。原位置聲骸仍會留在清單，未知資料不會猜測。

在同一次「開始辨識」中回頭查看先前聲骸時，工具只會在選中格、卡面與可見清單脈絡的
雜湊都吻合，而且名稱、等級、COST、主副詞條完整一致時接回原紀錄；證據不足就安全地
另建紀錄，不會只因詞條相同而合併兩顆聲骸。

掃描完後到側欄「聲骸」，預設依各角色需求將完整五件總分（滿分 500）由低到高排列，
也可改為由高到低。卡片呈現頭像、角色名與五個位置的分數；點角色卡或「查看配裝與詞條」
進入配裝分析。切換「低分單件」會依單件分數由低到高列出全部目前可評分裝備，以多欄卡片排列，隱藏不適用的
排序與養成篩選。角色模式保留等級、武器、技能、聲骸尚未完成及資料待補的篩選。
資料不完整時保留待評分及已評件數，不把未知當零分。玫瑰色表示未達角色需求（分數／套裝／
主詞條）；綠色表示分數至少 90 且沒有已知不符合需求的條件；其他分數採中性淡紫色。
+15、+20 的累積分仍會隨新詞條增加，不代表已開詞條品質一定差。

側欄「角色」直接進入養成查看模式。右側頭像可上下捲動，或展開成每排三名。
技能等級可雙擊修改、小型屬性節點可直接勾選，完成後按「儲存角色」。等級、武器和其他
少用資料收在「編輯角色資料」，數字欄不接受滑鼠滾輪變動。辨識會自動刷新角色與配裝，
同時保留尚未儲存的編輯內容。新角色名字可由高信心的裝配辨識加入，攻略與評分仍需另行更新。

不開遊戲也能點角色的「查看配裝與詞條」或低分單件，查看五件裝備、完整主副詞條、
數值檔位、角色需求權重和逐條得分。已收錄攻略的角色會依遊戲推薦與離線攻略判斷套裝與主詞條方向，
並顯示整套件數；手動覆寫收在「角色養成」的編輯模式／配裝管理，一般查看不必逐件確認。
選「以此聲骸為目標開始替換」會切到強化頁並保留該角色、位置、目前分數與需求。
可使用滑鼠側鍵四、Alt+← 或左側返回按鈕回到原配裝位置。
之後自行開遊戲並啟用辨識，新件會與該位置使用相同角色需求比較；COST 不同、拆套、
資料不足和仍有強化空間會分別說明。比較不會自動更換裝備，也不改候選品原本的歸屬。

「設定 → 歷史紀錄」預設折疊重複掃描：名稱、等級、COST、完整主副詞條數值都相同，且沒有
套裝或手動裝備位置衝突時，視為疑似同一件。取消勾選可查看每筆原始掃描；不刪除資料，
也不改寫強化／消耗事件。這項清單分組獨立於上面的即時 Session 身分判定。

若顯示「未看到支援的聲骸頁」，先停止辨識，改選另一個標有左側／右側與主要螢幕的項目。
設定頁可預覽不含畫面的診斷包；`application.log` 只記錄螢幕幾何、頁型、confidence、狀態與
錯誤類型，不記錄完整畫面、OCR 文字、角色名稱或本機檔案路徑。

v1.0.11：辨識開始/停止與狀態維持精簡列，螢幕與評分設定可展開；替換目標固定在上方，
副詞條直接顯示得分，完整權重與信心可查看提示。進出強化頁會獨立重新分類，不再等待舊版面。
穗穗加入手動提供的遊戲內使用率參考；新預設採官方調諧機率與實用品質基準，詳見
[評分方法](scoring-methodology.md)。既有自訂設定與歷史決策保留原版本。

## 安全邊界

帳號安全高於所有功能。本 repository 只包含離線、被動、唯讀的影像解析與本機養成
管理程式碼。

明確禁止：

- 控制或模擬滑鼠、鍵盤、手把；依賴中不得加入 `pyautogui`、`pynput`、AutoHotkey、SendInput 包裝器或巨集工具。
- 讀取遊戲記憶體、取得遊戲程序 Handle、注入 DLL、掛接繪圖管線、將覆蓋層注入或附掛遊戲。
- 攔截或分析封包，或讀取、修改、逆向遊戲檔案。
- 自動選擇、切換、強化、調諧、鎖定、棄置或消耗聲骸。
- 任何戰鬥、自動遊玩或對局輔助。
- 雲端 OCR、畫面上傳、遙測、完整影片錄製或完整即時畫面持久化。
- 把畫面右下角「特徵碼」當成聲骸 ID。

離線 CLI 只讀取命令列明確指定的本機圖片。即時模式只能在使用者手動開始後被動擷取
指定的整個顯示器，在記憶體中立即複製必要 ROI、釋放完整 frame，並把結構化結果顯示於
雙螢幕完整工具或本工具自己的單螢幕小面板。小面板不附掛遊戲，並排除於自身擷取。Milestone 3 已提供經測試的 monitor-only capture、ROI lease、
穩定門與 live OCR runtime；Milestone 4 已將手動開始／停止、持久 Session、人工修正 audit
與可搜尋歷史紀錄接上桌面頁面。
Milestone 5 已加入四種資料驅動評分模板、可編輯門檻與權重、實際 Roll 分級、剩餘詞條
枚舉，以及會保存到本機資料庫的強化決策。未知詞條、未知 Roll 或低 confidence 一律顯示
「需要人工確認」，不會為了產生分數而猜值。
V1.0.4 將通用輸出型更新為經實際強化紀錄校準的 V2，並分開顯示「已開詞條品質」、
「以 +25 五詞條為基準的副詞條累積分」與「預估完成分數」。這些分數只評副詞條；套裝、
COST 與主屬性是否適合角色仍是獨立條件。沒有角色專用設定時會明示「通用參考」。
V1.0.5 修正聲骸選擇頁雖已分類成功，卻因 15 個卡片角落必須同時穩定而長時間停在
「畫面切換中」的問題。卡片裝備證據仍會解析，但穩定門改用文字欄位與唯一選中卡的
短期、純雜湊標記；不保存卡片或完整畫面。
V1.0.6 減少小型文字區域的 OCR 放大處理，並排除裝配角色文字後方的深色動畫對穩定判定
的干擾。加入裝配角色名稱辨識、本機角色自動建立，以及聲骸保留與角色位置的同步流程。
V1.0.7 自動登記辨識到的裝備與明確位置，統一角色名稱分隔符號，拆開長聲骸名稱中的等級，
並從「合鳴效果」讀取套裝名稱。截圖工具通知遮住資訊時會略過該畫面。
V1.0.8 改以角色頭像與聲骸分數呈現總覽，可切換低分單件及養成篩選，並折疊重複掃描。
Milestone 6 已加入可日常編輯的角色 master/detail 頁面、武器、五項技能目標、十個小型
屬性節點、三人隊伍，以及每名角色獨立的五個永久聲骸位置。角色完成度、及格件數、
最弱位置與下一步皆由可測試的純領域規則計算；未確認符合目標的套裝／主詞條、不同評分
設定檔版本的分數，以及已消耗聲骸都不會取得完成 credit。
Milestone 7 已接入可手動維護的素材、庫存與養成目標，並以 breadth-first 規則產生全帳號
佇列、今日第一／第二順位、刷取次數、本週缺口與完成日估計。未知成本、平均產出或開放日
會要求人工確認；本機 JSON 素材包必須先通過版本／衝突預覽，且不會自動連網下載。
Milestone 8 已完成活動與待辦的每日／每週重複、到期分類、完成與延後，也加入全手動的
星聲、抽券、目標與未來收入規劃。抽卡三情境只採使用者明確輸入的需求抽數；墊抽與保證
狀態只保存、不會被當成未經確認的機率或需求推導。
Milestone 9 已完成首次啟動精靈、一次性被動 capture 測試、可選評分模板、SQLite／JSON
備份、預覽後 staged restore、原子 rollback、診斷包 allowlist、示範資料隔離，以及不會
下載模型的 PyInstaller one-folder 版本。版本與永久安全邊界可在設定頁「關於」查看。
設計與實作對照見
[`docs/passive-live-capture-design.md`](passive-live-capture-design.md)。

## 支援範圍與非目標

- Windows 10 2004 或更新版本、Python 3.12 x64。
- 《鳴潮》PC 國際服、繁體中文、1920x1080 或 2560x1440 無邊框滿螢幕。
- 頁面：角色聲骸總覽、聲骸選擇、聲骸強化、強化素材選擇、未知頁面。
- 強化資訊：名稱、等級、可見 COST、兩條主屬性、最多五條已解鎖副詞條、鎖定槽位、
  逐欄 confidence 與原始 OCR。聲骸選擇頁會交叉檢查選中卡的角色肖像徽章、右下
  `…裝配中` 與 `卸下／裝配`，輸出三態裝備欄位；證據衝突時仍輸出 `null`，且不會
  據此猜測角色身分或永久槽位。
- 詞條名稱 confidence 只會對已量測的 UI 裝飾字元與圖示前綴進行語意校準，並要求同列
  數值具有高信心；模糊詞、弱數值、缺 COST、缺主屬性或副詞條階段不完整仍要求人工確認。
- Milestone 2 提供繁中深色桌面骨架、八頁導覽、Dashboard、持久設定與 SQLite V1 schema。
- 被動 live runtime 支援 Fake、使用者圖片序列與 MSS 顯示器 backend，取樣限制 2–8 Hz；
  完整 frame 只存在 capture lease，後續元件只收到 allowlisted owned ROI。
- 桌面 live 工作區可選顯示器、顯示 Capture 狀態、OCR confidence、目前詞條、Session
  時間線、0–100 分數 breakdown、最終分數範圍與繼續／停止建議。辨識結果立即保存；
  歷史頁依「辨識批次」與單顆 Session 分區，不會把同次 Capture 的其他聲骸事件混入。
  同一次 Capture 的 A→B→A 回訪只有在視覺證據與完整結構化簽名都唯一吻合時才接回 A；
  不保存卡片像素，也不把雜湊當成跨 Capture 的遊戲內唯一 ID。既有歷史不會被自動回填、
  合併或刪除，新規則只作用於之後的辨識。
- 設定頁提供通用輸出、治療／生命、防禦與共鳴效率需求模板；系統模板可複製後調整，
  分數門檻、權重與預設設定檔皆保存於 SQLite。
- 角色頁可新增、編輯、封存與還原可能上場角色，調整優先度與評分設定檔，並管理武器、
  技能、隊伍、節點和五顆不共用的永久聲骸。
- 「養成規劃」固定並列技能現況／目標與無音區推薦，不需選日期或填素材庫存。角色技能可用提前／延後按鈕排序並儲存，規劃同步沿用。推薦沿用角色專屬聲骸評分，
  再按實際主輸出／副輸出／治療功能、必要配裝及雙套裝受益排序；加倍活動獨立顯示。
- 「抽卡規劃」顯示當期角色與結束倒數、下一期角色與開啟倒數；缺乏精確公告時保留待確認。
  星聲、墊抽、收入與素材舊資料保留，日常頁面不再要求維護。
- 「活動與待辦」以雙欄卡片依截止時間排列進行中活動，未開始活動預設收合。海墟／深塔獨立倒數與勾選完成；距結束不足 10 天的週末提醒尚未完成項目。
  也保留可展開的舊自訂待辦。Windows 背景提醒預設時間為六、日 10:00（UTC+8），設定提供時／分選擇器，可調整或停用。
- 設定可開啟啟動時及每 6 小時的公開資料更新；離線保留快取與來源時間。只傳送公開 GET，
  不傳送帳號、角色、截圖或資料庫。提醒需要電腦開機且登入，會遵守 Windows 通知設定。
- 不包含戰鬥模擬、完整 Optimal Stopping 或任何遊戲操作。

解析度、語言或 UI 版面不符時會回傳 `unknown` 或欄位 `null`，不會縮放到錯誤 ROI 或猜值。OCR confidence 是引擎的未校準行級分數，不等同真實機率。

## 安裝

系統的預設 `python` 可能不是 3.12，請明確建立 3.12 virtual environment：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

若不啟用 virtual environment，請在每個指令明確使用
`.\.venv\Scripts\python.exe`。本專案拒絕在 Python 3.13 執行。

核心 OCR 為 RapidOCR 3.9.2 + ONNX Runtime CPU。adapter 只載入 RapidOCR wheel 內的本機模型，模型缺失時會直接失敗，不會在執行期間下載或改用雲端服務。PaddleOCR 只保留為未安裝的 optional A/B 候選。

## 桌面應用程式

啟動一般可調整大小的繁中桌面視窗：

```powershell
.\.venv\Scripts\python.exe -m wuwa_assistant.app
```

第一次一般啟動會顯示三頁繁中快速開始，選擇單／雙螢幕模式、解析度及遊戲所在顯示器並測試，
不要求先建立角色、理解資料路徑或選擇評分模板。這些可選設定可在進入工具後再處理。
桌面版會在啟動時建立／更新本機
SQLite schema，提供總覽、養成規劃、角色、聲骸、聲骸強化、
抽卡規劃、活動與待辦、設定等八頁導覽；歷史紀錄在設定分頁。Capture 與 OCR 不會隨程式啟動；
只有使用者在「聲骸強化」頁按下「啟動小面板」或「開始辨識」後才會啟用，停止後仍保留已確認的結構化
Session、observation、event 與修正 audit。

資料預設存放於：

```text
%LOCALAPPDATA%\WuwaDisplayAssistant\
├─ data\wuwa-personal-assistant.sqlite3
├─ backups\database\
├─ backups\restore-staging\
├─ backups\restore-rollbacks\
├─ exports\
├─ exports\diagnostics\
├─ debug\low-confidence-rois\
├─ cache\public-data\official-feed.json
├─ logs\application.log
└─ config\
```

設定頁的資料按鈕可建立一致性 SQLite 備份、版本化 JSON 匯出、預覽並暫存還原、預覽
診斷包，以及建立／清除隔離的虛構示範資料。匯入不會熱替換已開啟的資料庫；下次啟動會
先驗證 staged snapshot、建立 rollback，再做同磁碟原子替換。詳見
[`docs/data-management.md`](data-management.md)。

## 一鍵腳本與 Windows 封裝

```powershell
.\scripts\setup.ps1
.\scripts\run.ps1
.\scripts\test.ps1
.\scripts\build.ps1
```

一般使用者可執行 `dist\WuwaDisplayAssistant\WuwaDisplayAssistant.exe`；整個
`WuwaDisplayAssistant` 目錄必須一起保留。封裝已內含 Qt plugin、Alembic migration、
評分資料、時區資料與 RapidOCR 本機 ONNX 模型，首次啟動不會下載模型。完整說明見
[`docs/windows-packaging.md`](windows-packaging.md)。

啟動 smoke test 可使用隔離資料目錄；它不會顯示視窗，也不會載入 capture 或 OCR：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m wuwa_assistant.app `
  --data-root "$env:TEMP\wuwa-assistant-smoke" --smoke-test
```

封裝後可單獨驗證三個本機 OCR 模型與 ONNX Runtime，不會建立資料庫、讀取螢幕或保存畫面：

```powershell
.\dist\WuwaDisplayAssistant\WuwaDisplayAssistant.exe --ocr-self-test
```

## Private samples

把使用者提供的原圖放入 `samples_private/`。這個目錄與 `debug_output/` 都已加入 `.gitignore`；原始截圖不得加入 commit。

本機可放置以下 optional ground truth：

```text
samples_private/
├─ 1.png
├─ ...
├─ 9.png
└─ expected.json
```

公開版不散布私人校準真值；JSON 比對行為可參考[合成評估測試](../tests/test_evaluation.py)。expected data 只用於事後差異與 accuracy 計算，不參與頁面分類、OCR 或 Session 判斷。

## CLI

啟用 virtual environment 後分析單張圖片：

```powershell
python -m wuwa_assistant analyze-image samples_private\3.png
```

分析依自然檔名排序的目錄：

```powershell
python -m wuwa_assistant analyze-sequence samples_private
```

也可明確傳入依序排列的檔案：

```powershell
python -m wuwa_assistant analyze-sequence `
  samples_private\1.png samples_private\2.png samples_private\3.png
```

寫入指定 JSON：

```powershell
python -m wuwa_assistant analyze-sequence samples_private `
  --output debug_output\acceptance.json
```

序列輸出包含：

- 每張圖片的 page type 與完整 `EchoObservation`。
- 每個欄位的 OCR／derived confidence、raw OCR token 與 bbox。
- 小型 debug ROI 與 OCR JSON 路徑。
- `EchoEvent` 時間線與完整 Session 狀態。
- 若有 `expected.json`，輸出逐欄 differences、exact accuracy 與 mean confidence。

debug writer 有尺寸 guard；目前驗收輸出的 ROI 最大為 680x610。它不會把 2560x1440 原圖複製到 `debug_output/`。

## 測試與品質檢查

```powershell
python -B -m pytest -q -p no:cacheprovider
python -B -m ruff check .
python -B -m ruff format --check .
python -B -m mypy src
python -m pip check
python -m wuwa_assistant.safety_scan
```

Committed tests 只使用合成 ndarray、fake OCR token 與 typed observations，不包含 private screenshots。

## 結構

```text
src/wuwa_assistant/
├─ app/        # PySide6 bootstrap、本機資料初始化與啟動錯誤處理
├─ ui/         # 繁中 Qt Model/View 視窗、Dashboard、設定與深色淡紫主題
├─ storage/    # SQLAlchemy 2、Alembic schema_version、repository 與 transaction
├─ capture/    # Fake／ImageSequence／MSS monitor backend、frame lease 與 ROI packet
├─ live/       # 時間穩定門、latest-only OCR worker、ROI analyzer 與 capture runtime
├─ application/# restart-safe Session、人工修正與歷史應用服務
├─ scoring/    # 資料驅動評分、Roll 分級、枚舉與規則型強化決策
├─ cultivation/# 角色、武器、技能、節點與永久聲骸完成度規則
├─ planning/   # breadth-first 佇列、素材缺口、開放日、週限次與完成日估計
├─ reminders/  # Asia/Taipei 到期分類、每日／每週 recurrence 與未知規則拒絕
├─ gacha/      # 手動資源、三情境收入與多目標抽數配置
├─ vision/     # ROI、OpenCV 前處理、頁面分類、pHash 與 StabilityGate
├─ ocr/        # 可替換 OCR Protocol、RapidOCR adapter、deterministic fake
├─ parsers/    # 繁中詞彙、嚴格數字解析、各頁面 observation parser
├─ state/      # 本機 Session 與事件追蹤
├─ models/     # typed dataclasses 與 enum
└─ cli/        # analyze-image / analyze-sequence
```

設計文件：

- [`docs/roi-plan.md`](roi-plan.md)：實圖校準 ROI 與穩定判定範圍。
- [`docs/ocr-strategy.md`](ocr-strategy.md)：離線 OCR 選型、信心與拒絕規則。
- [`docs/passive-live-capture-design.md`](passive-live-capture-design.md)：未來被動即時模式及兩階段素材確認。
- [`docs/acceptance-report.md`](acceptance-report.md)：九張 private samples 的實際結果。
- [`docs/product-plan.md`](product-plan.md)：V1 架構、Milestone、測試與 commit 閘門。
- [`docs/scoring-methodology.md`](scoring-methodology.md)：評分公式、資料來源狀態、機率假設與決策限制。
- [`docs/character-cultivation.md`](character-cultivation.md)：角色資料所有權、永久聲骸與完成度規則。
- [`docs/cultivation-planning.md`](cultivation-planning.md)：養成佇列、素材缺口、每日波片與本機靜態資料契約。
- [`docs/tasks-gacha-planning.md`](tasks-gacha-planning.md)：待辦 recurrence、到期分類與抽卡三情境的手動假設。
- [`docs/public-data-sync.md`](public-data-sync.md)：官方公開公告的手動 GET、日期拒絕規則、安全白名單與離線快取。
- [`docs/release-1.0.1.md`](release-1.0.1.md)：可用性救援版的修正、實機診斷與已知限制。
- [`docs/release-1.0.2.md`](release-1.0.2.md)：辨識後流程簡化、Session 分區與重複辨識修正。
- [`docs/release-1.0.3.md`](release-1.0.3.md)：裝備狀態校準、OCR 完整性與同次辨識回訪分組。
- [`docs/release-1.0.4.md`](release-1.0.4.md)：通用評分 V2、階段品質、完成期望與不完整結果防護。
- [`docs/release-1.0.5.md`](release-1.0.5.md)：聲骸選擇頁穩定門回歸修正與實機診斷證據。
- [`docs/release-1.0.6.md`](release-1.0.6.md)：辨識速度、裝配角色與保留至角色位置流程。
- [`docs/release-1.0.7.md`](release-1.0.7.md)：自動裝備登記、套裝、名稱修正與舊資料修復。
- [`docs/release-1.0.8.md`](release-1.0.8.md)：角色聲骸比較、低分單件、養成篩選、頭像與重複掃描折疊。
- [`docs/data-management.md`](data-management.md)：備份、匯出、staged restore、rollback 與診斷包契約。
- [`docs/windows-packaging.md`](windows-packaging.md)：PowerShell 腳本、one-folder build 與 installer 後續。

## v1.0.9 角色評分

角色總覽預設依角色需求選擇權重，自訂設定優先。八名角色的攻略來源、詞條順序、公式和限制可在卡片的「評分公式、需求與來源」查看。面板效率不足以確認時，在配裝分析集中說明；可依遊戲內推薦覆寫目標或選用個人設定。詳見 [角色評分與來源](character-scoring-sources.md)。

## v1.0.10 離線配裝分析

完整詞條、得分拆解、攻略配裝方向與替換目標流程見 [v1.0.10](release-1.0.10.md)。

## v1.0.11 實用評分與強化辨識

穗穗的需求參考採手動提供的遊戲內使用率；新預設評分與剩餘潛力採官方調諧機率。強化頁精簡控制區、固定替換目標並直接列出副詞條得分；進出遊戲強化頁可重新辨識，完整重掃可沿用已確認的裝備位置。使用方式、相容性與新版啟動路徑見 [v1.0.11](release-1.0.11.md)。

## v1.0.12 中間等級與投入成本

+17、+22 等級可正常辨識及評分；替換建議納入真正超越現有裝備的機率與下一節點素材成本。即時辨識改為只讀聲骸清單，培養完成後返回清單即可，並提供「立即重新辨識」。詳見 [v1.0.12](release-1.0.12.md)。

## v1.0.13 清單辨識恢復

修正已確認聲骸清單，卻被背景動畫卡在「畫面切換中」的問題。文字持續變動時會在選取穩定後重新讀取，連續兩次內容一致才保存；「立即重新辨識」也支援這條恢復路徑。詳見 [v1.0.13](release-1.0.13.md)。

## v1.0.16 九位角色的推薦與評分

整合九位角色的遊戲內使用率與重新查核的玩家攻略，補齊千咲專屬評分與 3＋2 混套判斷，調整莫寧、愛彌斯權重，保留其他已有依據的角色方向。角色需求頁可比較使用率、工具權重與來源；自訂設定保留。詳見 [v1.0.16](release-1.0.16.md)。

## v1.0.15 手動辨識與夢魘聲骸修正

聲骸強化可切換自動／手動辨識。手動模式按一下讀取目前聲骸，完成後停止擷取，換下一顆再按一次；逾時會返回待命。修正夢魘動畫連到選取框造成的辨識取消、停止時執行緒互等，以及夢魘名稱片段漏讀。詳見 [v1.0.15](release-1.0.15.md)。

## v1.0.14 穩定得分與投入建議

相同聲骸與評分條件的更新會保留已算好的副詞條得分；分析區突出累積分、期望、範圍、超越現有裝備的機率與分差。替換建議優先比較現有目標及後續成本，前期通常先觀察至 +15，仍保留不可能超越或提升太小的停止判斷。詳見 [v1.0.14](release-1.0.14.md)。

- [`docs/release-1.0.22.md`](release-1.0.22.md)：閃爍修正、側鍵返回、淡紫粉主題與角色／聲骸介面重製。

- [`docs/release-1.0.23.md`](release-1.0.23.md)：深色主題、全部低分單件多欄卡片、混搭套裝辨識別名修正。

- [`docs/release-1.0.25.md`](release-1.0.25.md)：聲骸分數只用數字色階，移除底層色塊。
- [`docs/release-1.0.27.md`](release-1.0.27.md)：夜色粉紫主題、聲骸並排明細、角色配裝摘要與大螢幕空間調整。
- [`docs/release-1.0.29.md`](release-1.0.29.md)：批次資料讀取、安全且可回應操作的關閉流程，以及成品自動驗證。
- [`docs/release-1.0.30.md`](release-1.0.30.md)：空白格動畫辨識、已裝備位置提示與評分期間保存修復。
- [`docs/release-1.0.28.md`](release-1.0.28.md)：整合夜色粉紫與無音區綜合推薦，固定穩定的 Windows 介面套件版本。
- [`docs/release-1.0.26.md`](release-1.0.26.md)：養成規劃、技能順序、效率建議、緊湊活動與海墟／深塔週期、時間選擇、歷史分頁及二選一按鈕。

## 兌換碼

側欄「兌換碼」會在啟動時及工具開啟期間每 30 分鐘檢查官方公告與公開彙整站，
新碼只在工具內提示，每組可一鍵複製。關閉工具後不檢查也不跳兌換碼通知。
可手動更新或關閉自動檢查；離線保留快取並標示來源與有效性待確認。
詳見 [v1.0.31](release-1.0.31.md)。

v1.0.34 統一聲骸分數的連續色帶：由低分紅橘色，經黃、綠、青藍到高分紫色；單件、五件總分、配裝均分、即時結果、預估分數、評分預覽與歷史列表使用同一套數值配色，待評分顯示灰色。總分除以5後對應色帶；及格／配裝適配另看文字。詳見 [v1.0.34](release-1.0.34.md)。

v1.0.33 修正同一顆聲骸與自己比較：改為自身強化建議，符合配裝且及格時明確建議升至 +25；不同顆雙方及格且候選落後不超過10分時，投入機率與平均提升門檻各減半。詳見 [v1.0.33](release-1.0.33.md)。

v1.0.32 在相同通知方框加入「本次啟動不再提醒」，並可逐組勾選「已完成」。
完成碼從預設清單隱藏，與已失效／過期碼一起保留於歷史，可取消完成。
詳見 [v1.0.32](release-1.0.32.md)。
