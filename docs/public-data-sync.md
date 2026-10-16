# 官方公開資料同步

V1.0.1 的公開資料同步是明確的使用者動作，不是背景服務。只有在設定頁按下「更新官方公開資料」後，程式才會向庫洛《鳴潮》繁體中文官網的公開 JSON 發出 HTTPS GET：

- `MainMenu.json`：取得公開公告索引。
- `article/{id}.json`：取得入選公告的公開文章內容，以確認文章內是否有唯一且明確的活動起訖日期。

程式不送出 request body、Cookie、Authorization、帳號識別碼、保底狀態、本機角色、遊戲畫面、遊戲 log 或資料庫內容。來源主機使用精確 allowlist，redirect 若離開 allowlist 會被拒絕；每個回應都有 timeout、最大 bytes、UTF-8 JSON 與欄位型別檢查。

## 日期拒絕規則

CMS 的 `startTime` 是文章發布時間，不是活動開始時間。`endTime=2099-01-01` 是哨兵值，不可視為活動期限。只有文章本文包含一個可確認的日期範圍時，才會輸出活動起訖日；相同範圍在角色／武器段落重複出現會先去重。若文章沒有日期、存在兩組不同日期，或 detail 無法讀取，UI 顯示「日期需人工確認」，不猜測。

## 快取與離線行為

已驗證資料以 atomic replace 寫入：

```text
%LOCALAPPDATA%\WuwaPersonalAssistant\cache\public-data\official-feed.json
```

快取包含 schema version、官方來源、更新時間、SHA-256、ETag、Last-Modified 與經過濾的公告欄位，不保存文章 HTML。啟動只讀取本機快取，不連線；伺服器回應 `304` 或更新失敗時繼續使用上一份 known-good cache。載入快取時會再次驗證分類、日期狀態、官方來源與每個可點擊連結，避免被修改的快取建立任意外部連結。

「官方卡池公告」與「我的資源與保底」是兩組不同資料。公開同步不會新增、刪除或改寫個人墊抽、保證狀態、星聲、抽券、目標或預估收入。

真實端點 smoke 預設不執行，避免測試無聲連網；需明確設定後執行：

```powershell
$env:WUWA_OFFICIAL_DATA_SMOKE = "1"
.\.venv\Scripts\python.exe -m pytest tests\public_data\test_official_endpoint_smoke.py -q
```
