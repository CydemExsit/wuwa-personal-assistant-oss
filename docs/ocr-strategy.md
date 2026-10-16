# 離線 OCR 策略

## 範圍與安全邊界

第一階段只辨識使用者主動提供的靜態圖片，而且只把已裁切的 ROI 傳入 OCR。
`OcrEngine` 公開介面僅接受記憶體中的 `numpy.ndarray`；不接受檔案路徑、URL、
遊戲視窗或程序資訊。OCR 層不負責螢幕擷取、寫檔、輸入控制或網路請求。

RapidOCR adapter 明確指定套件內建的三個本機模型檔。模型遺失時立即失敗，不啟用
RapidOCR 的下載回退，因此執行 OCR 時不會呼叫雲端服務，也不會傳送任何畫面：

- `PP-OCRv6_det_small.onnx`
- `ch_ppocr_mobile_v2.0_cls_mobile.onnx`
- `PP-OCRv6_rec_small.onnx`

完整截圖不可寫入 `debug_output/`。只有裁切後的小 ROI、其前處理版本，以及原始 OCR
JSON 可供除錯；信心足夠時不需要保存 ROI。

## 抽象介面

```python
class OcrEngine(Protocol):
    def recognize(
        self,
        image: NDArray[np.uint8],
        *,
        detect: bool = True,
    ) -> list[OcrToken]: ...
```

`OcrToken.bbox` 是 ROI 內的 `(x1, y1, x2, y2)` 軸對齊座標，右下界不包含在內。
`confidence` 保留引擎回傳的文字行信心值，不先四捨五入，也不把詞彙比對分數混入
原始值。呼叫端必須保留 token，才能區分「OCR 看到了什麼」與「parser 接受了什麼」。

`FakeOcrEngine` 提供兩種 deterministic 測試回應：

1. 依 ROI 內容 SHA-256 key 固定回傳，適合同一 ROI 反覆解析。
2. 未命中 key 時依 queue 順序回傳，適合模擬多個 ROI 或時間序列。

## 引擎選擇

| 引擎 | PoC 角色 | 優點 | 風險 |
| --- | --- | --- | --- |
| RapidOCR 3.9.2 + ONNX Runtime CPU | 預設 | 輕量、離線、Windows/Python 3.12 可用、PP-OCRv6 初測效果良好 | raw confidence 不是校準機率；非內建模型可能觸發下載，因此 adapter 禁止回退 |
| PaddleOCR 3.7.0 + ONNX Runtime | optional A/B 與回退 | 官方支援 `chinese_cht`，可交叉驗證難字 | PaddleX 相依較大，3.x API 變動較快，必須隔離在另一個 adapter |
| 固定字元模板 | 僅數字最後回退 | 完全 deterministic、無額外 runtime | 只適用固定解析度及 `0-9 + . %`，不可用來解析中文 |

PoC 不加入 Tesseract、EasyOCR、PaddlePaddle GPU 或 CUDA。先以九張 private screenshots
的逐欄 exact accuracy、錯誤接受率與拒絕率判斷是否真的需要第二個通用引擎。

## RapidOCR 呼叫方式

`RapidOcrEngine` 延遲到第一次 `recognize()` 才載入 RapidOCR 與 ONNX 模型；頁面分類、
測試及不需要 OCR 的 CLI 路徑不承擔模型初始化成本。所有遊戲文字均為水平文字，故固定
停用方向分類，並以 `text_score=0.0` 取得低信心原始候選，由 parser 決定接受或拒絕。

- `detect=True`：用於含一行以上文字、文字位置仍可能浮動的 ROI。保留 detector 的
  四點框，再轉成 `OcrToken` 所需的軸對齊框。
- `detect=False`：用於已緊密裁切的單一名稱、等級或數值列。辨識器沒有 detector
  座標，因此 token bbox 是完整 ROI `(0, 0, width, height)`。

引擎輸出文字、score 或 box 數量不一致時視為 backend error，不補框、不猜值。空白
ROI 或沒有辨識結果則回傳空 list，後續欄位輸出 `null`。

## ROI 與前處理

禁止對 2560x1440 原圖做全文 OCR。先依 `docs/roi-plan.md` 分類頁面，再裁切名稱、等級、
兩條主屬性及五個副詞條。每一欄先保留原色版本，再視低信心狀況產生下列小 ROI：

1. 2x 或 3x Lanczos/Cubic 放大。
2. 灰階加 CLAHE。
3. Otsu 或 adaptive threshold；避免把細筆畫侵蝕掉。

固定單列優先走 `detect=False`。只有整塊副詞條或頁首 ROI 需要 detector；這可避免背景
星點、圖示和鎖定提示被當成文字。

## 數字與單位

RapidOCR 的 `rec_keys_path` 是模型類別字典，必須和模型輸出維度一致，不能拿來當 runtime
字元白名單。數字欄採專用解析流程：

- 等級：`^\+?(0|5|10|15|20|25)$`
- COST：`^[134]$`
- 百分比：`^\d{1,2}\.\d%$`
- 固定整數：`^\d{1,5}$`

只允許去除首尾空白及正規化全形的 `＋％．０-９`。不得先刪除非法英文字母再把剩餘內容
強轉為數字，因為這會把錯誤 OCR 變成看似合理的結果。建議至少兩個前處理版本輸出完全
一致，且均符合欄位 grammar 後才接受；否則數值或單位為 `null`。

## 繁體中文詞彙與模糊比對

屬性名稱先保留 raw text，再比對繁中詞彙表。exact match 優先；模糊比對只有在最佳候選
距離低於門檻，且明顯優於次佳候選時才可使用。下列詞彙必須各自保留：

- `攻擊`、`暴擊`、`暴擊傷害`
- `普攻傷害加成`、`重擊傷害加成`
- `共鳴技能傷害加成`、`共鳴解放傷害加成`

「攻擊」是否為百分比不可由名稱猜測；要由獨立辨識成功的 `%` 單位決定。詞彙表校正後
仍需保存 `raw_name`，並降低 derived confidence；不確定時輸出 `null`。

## Confidence 與拒絕規則

引擎 score 是未校準的行級分數，不可當作真實正確率，也不可直接比較不同 OCR 引擎。
每個欄位建議保存：

- `raw_ocr_confidence`
- `preprocessing_agreement`
- `vocabulary_similarity`（只適用文字名稱）
- `derived_field_confidence`

初始保守門檻可使用：數字 raw score 至少 `0.85`、grammar 成立且至少 2/3 前處理一致；
中文 exact match raw score 至少 `0.75`。這些門檻必須用 private fixtures 校準。derived
confidence 可取必要證據的最小值，但不得覆寫 raw score。必要證據未通過就輸出 `null`，
不依遊戲成長規則或前後畫面補猜。

## 相依性與部署注意事項

- 專案固定 Python 3.12；機器的預設 `python` 可能是 3.13，應使用 `.venv` 的直譯器。
- ONNX Runtime Windows build 需要 Microsoft Visual C++ 2019 runtime。
- RapidOCR 依賴 `opencv-python`；不要同時安裝 `opencv-python-headless` 造成兩套 `cv2`。
- 第一階段只使用 CPU provider，避免 CUDA/DirectML 驅動差異。
- 未來即時模式仍只把記憶體中的小 ROI 傳給同一介面；完整桌面 frame 不落盤。

參考：[RapidOCR 使用文件](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/)、
[PaddleOCR OCR Pipeline](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html)、
[ONNX Runtime 安裝需求](https://onnxruntime.ai/docs/install/)。
