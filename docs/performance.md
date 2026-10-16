# 效能量測與架構（v1.0.35）

這次依序處理辨識、評分、畫面與啟動，使用 v1.0.34（`8fda87c`）作為相同機器、相同資料與相同依賴的基準。數值是本機觀測，不是所有電腦的速度保證，也不作為 CI 的固定毫秒門檻。

## 量測環境與範圍

- Windows、Intel Core i7-12700K（12 核／20 邏輯處理器）、Python 3.12.10。
- NumPy 2.5.1、PySide6 6.11.1、RapidOCR 3.9.2、ONNX Runtime 1.28.0、SQLAlchemy 2.0.51、Alembic 1.18.5。
- 評分使用明確的合成詞條，涵蓋目前與舊版 Roll 資料；OCR 使用本機 11 張既有私人圖片。圖片與 OCR 文字不放進效能報告或版本庫。
- 單項基準在獨立程序執行，重複 3–7 次；UI 使用 50 位合成角色／250 件聲骸，另做 50 次更新。原始時間樣本、median、p95、CPU 與程序記憶體由腳本輸出。
- 完整流程重播涵蓋記憶體 frame 建立、ROI、指紋、頁面判斷、辨識、SQLite 保存、評分與 Qt 更新。**不包含 Windows 實體擷取驅動或原有 600 ms 穩定等待**；正式程式仍保留穩定等待、最新任務優先與過期結果淘汰。
- 實體擷取另以 `benchmark_capture.py` 獨立測量。30 次 2560×1440 MSS 擷取的中位數／p95：舊版 36.16／51.90 ms，新版 36.89／55.84 ms；ROI 擷取約 4.24／4.44 ms，指紋約 7.27／7.28 ms，這些未變動階段沒有加速主張。隔離指紋與 packet 建立後，coordinator 判定中位數約 0.017／0.019 ms。600 ms 是最低穩定時間，5 Hz 採樣及排程可使實際等待到約 0.6–0.8 秒；最後一組兩版實測 812／813 ms，皆只排入一個工作、拒絕 31 次重複狀態。像素僅暫存在記憶體，沒有 OCR、保存或傳出。
- 啟動基準執行獨立程序的 `--smoke-test`，只報告程序起訖耗時，不把父程序 CPU／記憶體當成應用消耗；不初始化完整 presenter，也不啟動擷取，不能當成所有角色載入完畢的時間。

## 主要結果

| 情境 | v1.0.34 | v1.0.35 | 說明 |
| --- | ---: | ---: | --- |
| +5 通用輸出機率評分，首次計算 | 1,744 ms | 100 ms | 約 17 倍；精確枚舉 |
| 同一 +5 評分重算，中位數 | 1,735 ms | 1.27 ms | 重用分數機率分布 |
| 完整 11 圖離線重播，三輪中位數 | 1,057 ms | 59 ms | 30 個相關頁面；包含重複圖片 |
| 上述流程第一輪中位數 | 1,156 ms | 585 ms | 同輪共用 ROI 與評分也受益；不是純 OCR 時間 |
| 50 位角色初次建立卡片 | 260 ms | 97 ms | 延後建立隱藏的單件視圖 |
| 相同配裝資料更新，中位數 | 266 ms | 0.14 ms | 保留既有 widget |
| 單一角色變更 | 269 ms | 6.43 ms | 只重建變更卡片 |
| 總分／單件切換，中位數 | 329 ms | 22.48 ms | 已建立的卡片可以重用 |
| 50 次 UI 更新後 RSS | 258.9 MB | 201.0 MB | 約減少 22%；十進位 MB |
| 原始碼既有空資料庫啟動，中位數 | 2,144 ms | 1,741 ms | 約減少 19%；仍做健康檢查 |
| Windows 成品既有空資料庫啟動，中位數 | 1,752 ms | 1,640 ms | 兩版各 7 次；約減少 6.4% |
| Windows 成品資料夾 | 367.54 MB | 358.39 MB | 減少額外 Qt 翻譯；保留 hook 基本翻譯、本機模型與 DLL |

純辨識第一張新圖片仍需模型推論，大部分約 0.44–1.19 秒。完全相同 ROI 的重複處理由原先數百毫秒降至約 0.15–32 ms（含不同頁面的解析工作）；這個數字不能代表每顆新聲骸的首次辨識時間。+20／+25 等本來只有約 1 ms 以下的評分，沒有相同比例的改善。

## 四個實作邊界

1. `scoring/projection.py` 將剩餘 Roll 的笛卡兒積運算移到 NumPy 原生批次，每批最多 32,768 個結果。維持原機率、Python 3.12 補償加總與小數捨入邊界；接近半分位的結果使用原本的 scalar 計分。最多快取 32 組、每組 10,001 格分數機率質量（陣列約 2.56 MB 上限，另有少量 key）。快取 key 含實際權重與情境推得的數值；目標分數與門檻另外歸約，不會因沿用 profile ID 取得過期結果。
2. `ocr/cache.py` 僅在 ROI 位元組、形狀和 detection mode 完全相同時重用結果。預設最多 64 項／1 MiB 保守 token 估算，不保留影像。每次交付 token 副本，原始信心值不變；空結果也有同樣的限制。重新檢查、重設及停止會清除；generation 防止清除前的推論重新填入舊快取。模型不因此重新載入。
3. `ui/widgets/keyed_grid.py` 依穩定識別碼與不可變 view snapshot 比對，重用相同卡片，替換變更卡片，刪除不存在的卡片；隱藏頁面在切入時建立。排序、篩選、欄數、選取、點擊導覽仍由原頁面管理。
4. SQLite 啟動先做健康檢查。已達目前 schema head 才省略 Alembic 與第二次檢查；新建、舊版與未知 revision 仍走遷移路徑。測試會核對快速路徑常數與實際 Alembic head，避免日後加 migration 時漏更新。

實驗中的全域縮小 OCR detector 會改變辨識結果；降低到 1／2 個推論執行緒也較慢，均未採用。既有小型錨點的 detector 調整維持原樣。

## 語言與原生核心的正式比較

另外實作 `scripts/experiments/projection_native.cpp`，以 C ABI／ctypes 連接 Python，使用 g++ 15.2.0、`-O3 -fno-fast-math -ffp-contract=off`。C++ 自己枚舉組合並保留補償加總，接近小數捨入邊界時回呼 Python 原公式；完整測量包含 NumPy 陣列轉換、FFI 與回呼成本。

| 未快取計算 | NumPy 核心 | C++ 原型（含轉換） | PyInstaller 成品的 NumPy／C++ |
| --- | ---: | ---: | ---: |
| +5，目前規則 | 93.96 ms | 35.34 ms | 96.63／35.38 ms |
| +10，目前規則 | 10.63 ms | 1.71 ms | 11.16／1.71 ms |
| +5，舊版規則 | 98.50 ms | 39.12 ms | 96.80／39.32 ms |

8 個完整目前／舊版 projection 與 20,000 個小數邊界輸入相同，獨立 PyInstaller 成品也重新通過；106,806-byte DLL 在封裝前後雜湊一致，只依賴 Windows 系統 UCRT。將原型接到完整三輪離線重播後，辨識／評分／決策仍一致；流程中位數 NumPy 59.13 ms、C++ 55.38 ms。這是分開程序的觀測，不能把全部差距都歸因於核心。

**本版採用 NumPy 原生批次，保留 Python／Qt。** C++ 對未快取核心確實更快，但目前首次重度評分已降至約 0.1 秒，重用約 1 ms；主要剩餘等待是新畫面的 OCR 與穩定閘門。對目前流程，新增編譯器、DLL／ABI 與 Python 回呼的維護成本，高於再縮短數十毫秒首次運算的收益。原型保留供未來大量未快取評分工作評估，沒有作為正式 runtime；沒有聲稱比較過完整 Rust／C++ UI 重寫。

## 正確性、資源與視覺驗證

- 28 組目前／舊版／角色設定、兩種情境及多種目標，共 507 案例，完整 projection 與原始引擎精確相同。另保存 10 個合成基準結果作為 CI regression fixture，測試小數邊界、門檻與分布快取。
- 11 張私人圖的分類、欄位與原始信心值雜湊相同。三輪完整重播的 observation、projection 與 decision 全部相同；低信心、未知值與人工確認規則保留。
- 混合圖片重播 30 輪／330 張輸入：630 次 frame lease 全數釋放，最高同時 1 個；OCR 快取固定 46 項／58,728 bytes，總推論 miss 固定 46 次。Python 執行緒數維持 1。RSS 404 → 451 MB；期間追蹤 session 由 3 增至 61，保留歷史會占用記憶體，不能解讀成整個程序記憶體固定不增長。
- 固定圖片重播 200 次：400 次 lease 全數釋放，快取固定 4 項／4,476 bytes，session 固定 1；RSS 276 → 287 MB，包含強制寫入與報告累積的負擔。停止後的一秒閒置 CPU 觀測為 0；此短測量不是整天運行保證。正式 coordinator 對相同觀測會省略重複保存，重播故意逐次呼叫保存以驗證資料路徑。
- 用原始資料庫的只讀備份比對角色、角色詳情、兩種配裝、農取建議及原始／分組歷史等七個 service 輸出，全部相同；資料副本通過 integrity／foreign-key check，原始資料庫內容指紋未變。
- 原生 Windows Qt 實測 DPR 1.25／2.0。舊版各三種與新版各七種 UI 狀態通過；檢查總覽、單件、詳情、角色、即時／預覽／歷史數字及透明背景，只取完整可見的 viewport。擷取全程關閉。
- `benchmark_soak.py` 補上 5 Hz 的長時間實際背景排程：固定畫面、每五秒切換圖片、最後反覆停止／啟動，使用真正的 OCR worker、SQLite、評分與 Qt 更新。每 15 秒記錄 CPU、RSS、Python 執行緒、active／pending 工作、UI 佇列、模型載入次數、cache 及 widget 數量。每次停止確認 worker 結束、frame 全部釋放、cache 清空，最後驗證資料庫。原始圖片只作記憶體輸入，資料庫是暫存副本；這與直接快速循環的短重播分開，不能用短測試代替長時間穩定性。

兩版各完成 900 秒持續運作，均保存 132 個結果、完成 5 次啟停且沒有分析或回呼錯誤。舊版 4,502／新版 4,501 個 frame lease 全數釋放；模型各載入一次，運作時 Python 執行緒均為 3 個，停止後只剩主執行緒，Qt widget 維持 199 個。新版 OCR 快取最多 46 項／58,728 bytes，每次停止均清空，資料庫健康檢查通過。

| 持續運作資源 | v1.0.34 | v1.0.35 |
| --- | ---: | ---: |
| 固定畫面 CPU，核心秒／秒 | 0.149 | 0.144 |
| 輪流換圖 CPU，核心秒／秒 | 1.031 | 0.281 |
| 程序 RSS 峰值 | 430.42 MB | 424.55 MB |
| 最後取樣 RSS（停止前） | 411.51 MB | 409.54 MB |

輪流換圖階段（第 210–705 秒）平均 CPU 消耗減少約 73%；啟動擷取前的五秒閒置觀測皆為 0。兩版同時執行以觀測各程序資源，這些數據不作為隔離環境的延遲比較。RSS 包含保留歷史及報告資料；15 分鐘內執行緒、widget 與佇列有界，仍不能推論整天運作的記憶體固定不增長。

## 重跑方式

在相同機器、沒有其他重負載時分別跑每套基準。舊版可把 `git archive 8fda87c src` 解至隔離目錄，再將 `PYTHONPATH` 指到該目錄的 `src`；使用同一份新版 benchmark 腳本作比較，勿替換原始碼工作目錄或使用真實資料庫作寫入測試。

```powershell
.venv\Scripts\python.exe -B scripts\benchmark_performance.py scoring --output work/scoring.json
.venv\Scripts\python.exe -B scripts\benchmark_performance.py recognition --samples samples_private --output work/recognition.json
.venv\Scripts\python.exe -B scripts\benchmark_performance.py ui --output work/ui.json
.venv\Scripts\python.exe -B scripts\benchmark_performance.py startup --output work/startup.json
.venv\Scripts\python.exe -B scripts\benchmark_replay.py --samples samples_private --rounds 30 --output work/replay.json
.venv\Scripts\python.exe -B scripts\benchmark_soak.py --samples samples_private --duration 900 --output work/soak.json
```

`--samples` 是已有本機 PNG 的目錄；腳本不會擷取畫面。所有 SQLite 寫入都用暫存資料根目錄。啟動基準可加 `--executable` 指定打包的 exe。保存前後 JSON 後同時比較輸出雜湊、結果與計時，不只比較最快的一次。正式交付的品質與打包驗證見 [v1.0.35](release-1.0.35.md)。

實體擷取基準必須明確指定 `--live-capture --monitor 1`（選擇實際 2560×1440 實體顯示器）：

```powershell
.venv\Scripts\python.exe -B scripts\benchmark_capture.py --live-capture --monitor 1 --output work/capture.json
```

C++ 原型須有本機編譯器；下列是已驗證的建置方式。測試成品是獨立 benchmark，不取代正式應用。PyInstaller 的 `--specpath` 會影響相對路徑，因此 DLL 使用絕對路徑；DLL 搜尋 PATH 依正式 build 腳本隔離至 Python／Windows 目錄。

```powershell
g++ -std=c++17 -O3 -fno-fast-math -ffp-contract=off -shared -static-libgcc -static-libstdc++ -o work/projection_native.dll scripts/experiments/projection_native.cpp
.venv\Scripts\python.exe -B scripts/benchmark_native_projection.py --library work/projection_native.dll --output work/native.json
$prototypeDll = (Resolve-Path work/projection_native.dll).Path
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onedir --name NativeProjectionBenchmark --distpath work/native-package --workpath work/native-build --specpath work --collect-data wuwa_assistant.data.scoring --add-binary "$prototypeDll;." scripts/benchmark_native_projection.py
work/native-package/NativeProjectionBenchmark/NativeProjectionBenchmark.exe --library work/native-package/NativeProjectionBenchmark/_internal/projection_native.dll --output work/native-frozen.json
```
