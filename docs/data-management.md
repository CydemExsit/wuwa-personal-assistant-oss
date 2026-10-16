# 本機資料、備份與還原

V1 的使用者資料只存放在使用者指定的本機根目錄；預設為
`%LOCALAPPDATA%\WuwaPersonalAssistant\`。Repository、private screenshots 與
`debug_output` 都不是正式資料位置。

## 備份與 JSON 匯出

- SQLite 備份使用 SQLite online backup API，產生不依賴 `-wal`／`-shm` 的一致性快照。
- JSON 匯出包含格式版本、應用程式版本、schema revision、UTC 時間、各資料表列數、
  schema SHA-256、database SHA-256 與 base64 SQLite snapshot。
- 匯入預覽會在任何寫入前驗證 JSON 結構、大小、hash、schema、`quick_check`、外鍵與列數。
- 來源檔在預覽後若有任何變更，確認 token 失效，不能建立待還原檔。

## Staged restore 與 rollback

匯入確認後只寫入固定的
`backups\restore-staging\pending-restore.sqlite3`，不會替換目前 SQLAlchemy engine 正在
使用的資料庫。下次啟動且 engine 尚未建立時，程式依序：

1. 再次驗證 staged snapshot、schema、外鍵、hash、symlink 與 sidecar。
2. 以 SQLite backup 建立 `backups\restore-rollbacks\pre-restore-*.sqlite3`。
3. 確認 live database 在 rollback 後沒有被第二個程式實例修改。
4. 確認 staged 與 live 位於同一 filesystem。
5. 使用 `os.replace` 原子替換 live database。

任何檢查或 Windows lock 失敗都會保留 live database 與 staged snapshot；程式不會自動
刪除、重設或猜測修復資料。資料庫損毀時，啟動畫面會顯示友善錯誤，使用者可保留原檔並
從已驗證的 JSON／SQLite 備份復原。

## 診斷包

設定頁會先列出診斷包的精確檔名、類別、大小與 SHA-256，只有使用者確認同一份預覽後
才建立 ZIP。Allowlist 只有 app version、無內容的 database health metadata 與本機文字
log；下列項目永久排除：

- SQLite database 或 JSON backup 內容；
- 完整 frame、遊戲截圖或影片；
- low-confidence debug ROI 與其他影像；
- 遊戲程序、帳號 API 或網路取得的資料。

診斷包只建立在本機 `exports\diagnostics\`，程式沒有上傳功能。

## 示範資料

虛構示範資料使用獨立 `kind=demo` dataset，不含使用者提供的角色、隊伍、Tier 或截圖。
建立動作可重複執行；清除只選取 demo dataset。若當下正在顯示 demo dataset，清除後主
視窗會先執行正常 shutdown 再關閉，避免 presenter 繼續向已刪除的 dataset 寫入；下次
啟動會安全回到主要資料。
