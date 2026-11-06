# 即時聲骸分析與刷取需求

- 聲骸強化頁即時顯示目前聲骸的評分、保存與升級建議。手動核對會更新目前結果。新一輪辨識從新的預覽開始。
- 需要保留角色配裝時，展開「套用到角色配裝」，選擇角色與位置後按「保存至角色位置」。這項動作才會記下該聲骸和配裝。既有角色、配裝和歷史可以照常查看。要逐格核對遊戲內的已裝備聲骸，請辨識並套用到相符的角色與位置。
- 在「聲骸 → 查看配裝與詞條」選位置並按「這個位置需要補充」。已有聲骸的位置沿用它的 COST。空位會請你選 COST。補到可培養的聲骸後，在同一位置按「取消補充需求」。
- 「養成規劃 → 聲骸刷取需求」依位置、COST、套裝、角色與隊伍優先級、配裝缺口排列來源。展開「查看推薦理由與資料待確認項目」可看受益位置、貢獻點與計算明細。
- 需求保存在目前資料集。舊的無音區庫存標記沒有角色或位置資訊，因此保留原始設定，改由使用者直接標記要補充的位置。

## 取得來源和資料範圍

國際服 3.7.8 的 37 個合鳴套裝共有 109 種已存在的套裝與 COST 組合，每種至少有一條已核對的刷取路線。目錄涵蓋既有 21 個無音區，以及首領、戰歌重奏、夢魘與殘象聚落。無音區和聚落依照掉落範圍配對。刷取建議排除野外吸收。排序點表示養成優先度。遊戲解鎖條件、當期掉落與個人進度以遊戲為準。

- 夢魘·凱爾匹，COST 4，願戴榮光之旅或流雲逝盡之空。前往七丘哀慟谷，在夢魘祓除追蹤。 [官方 2.4 更新](https://wutheringwaves.kurogames.com/en/main/news/detail/2801) · [位置](https://3g.ali213.net/gl/html/1666657.html) · [聲骸資料](https://wuthering.gg/zh-Hant/echos/nightmare-kelpie)
- 共鳴迴響·天演溯心，COST 4。銜夢照世之心可由夢州夢玄溪山的戰歌重奏「定序諸理之律」取得。 [官方 3.7 更新](https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/5562) · [官方攻略站配裝](https://wuwaguide.kurogames.com/zh-Hant/?role_id=1311&strategy_id=11096) · [副本位置](https://gamewith.jp/wutheringwaves/578962) · [掉落方式](https://game8.jp/meicho/820400)
- 設有位置套裝目標時優先使用該目標。沒有配裝判定且已辨識出目前套裝時，已標記的位置可依目前套裝查路線。未知或不存在的組合仍列入待確認項目。

來源資料取自既有無音區目錄採用的國際服正式版快照。建置腳本依聲骸、合鳴組、敵跡探尋、地圖標記和副本入口的識別碼關聯資料。少數敵人與掉落聲骸名稱不同，腳本記錄核對後的識別碼對應。輸出只收錄名稱、路線、套裝、COST 和來源，並附來源檔 SHA-256。 [固定資料版本](https://github.com/Arikatsu/WutheringWaves_Data/tree/9218d612ad815e398e064e577e42aaf878899968)

維護時以固定版本的公開資料目錄執行下列指令，確認所有套裝與 COST 組合的回歸測試通過後再更新兩版。應用程式使用隨附目錄。

```powershell
python scripts/build_echo_source_catalog.py <正式版資料目錄> src/wuwa_assistant/data/planning/echo-sources.v1.json
```

## 推送前預檢

在 Windows PowerShell 執行：

```powershell
.\scripts\preflight.ps1
.\scripts\preflight.ps1 -Package
```

第一個命令安裝具雜湊的 Python 3.12 鎖定環境，稽核相依套件，接著執行與 CI 相同的測試、Ruff、mypy、pip 和安全檢查。OSS 也執行公開資料界線及 Git 歷史憑證掃描。第二個命令另外編譯並驗證 Windows 安裝檔與免安裝包，需要 Inno Setup，可用 `ISCC_EXE` 指定編譯器。

- Qt GUI 測試曾在介面重建後選到待刪除的舊元件。查找應從目前的容器或模型開始，等待狀態成立。啟動、GUI 和其餘測試各用獨立 pytest 程序。
- OSS 安全稽核曾因 setuptools 的已知漏洞失敗。兩版預檢均用已鎖定並含修補的套件，以及和 CI 共用的稽核腳本。新漏洞會令檢查失敗。
- 私人版曾在封裝完成後因 Artifact 儲存配額失敗。一般 Push／PR 仍編譯並驗證完整封裝，結果寫入工作摘要。需要下載產物時，手動執行 CI 並勾選 `upload_packages`。產物保留一天，仍受 GitHub 配額限制。[GitHub Artifact 文件](https://docs.github.com/en/actions/tutorials/store-and-share-data)
