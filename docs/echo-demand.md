# 即時聲骸分析與刷取需求

- 聲骸強化頁即時顯示目前聲骸的評分、保存與升級建議。手動核對會更新目前結果。新一輪辨識從新的預覽開始。
- 需要保留角色配裝時，展開「套用到角色配裝」，選擇角色與位置後按「保存至角色位置」。這項動作才會記下該聲骸和配裝。既有角色、配裝和歷史可以照常查看。要逐格核對遊戲內的已裝備聲骸，請辨識並套用到相符的角色與位置。
- 在「聲骸 → 查看配裝與詞條」選位置並按「這個位置需要補充」。已有聲骸的位置沿用它的 COST。空位會請你選 COST。補到可培養的聲骸後，在同一位置按「取消補充需求」。
- 「養成規劃 → 聲骸刷取需求」依位置、COST、套裝、角色與隊伍優先級、配裝缺口排列來源。展開「查看推薦理由與資料待確認項目」可看受益位置、貢獻點與計算明細。
- 需求保存在目前資料集。舊的無音區庫存標記沒有角色或位置資訊，因此保留原始設定，改由使用者直接標記要補充的位置。

## 取得來源和資料範圍

無音區使用已核對的既有目錄，僅配對 COST 1／3。非無音區條目各自紀錄區域、合鳴套裝、COST 與來源。新來源可沿用同一組欄位加入。刷取排序點表示養成優先度。不同玩法的體力、時間和掉落率尚未納入比較。

- 夢魘·輝螢軍勢，貝奧海域，COST 4，高天共奏之曲或凌冽決斷之心。[官方 2.2 更新](https://wutheringwaves.kurogames.com/en/main/news/detail/2381)、[聲骸資料](https://wuthering.gg/echos/nightmare-lampylumen-myriad)
- 輝螢軍勢，虎口礦場，COST 4，凝夜白霜。[聲骸資料](https://zilliongamer.com/wuthering-waves/c/echeos/lampylumen-myriad)、[位置](https://nintendosmash.com/wuthering-waves-how-to-get-to-lampylumen-myriad-boss-location/)
- 無常凶鷺，荒石高地，COST 4，輕雲出月。[聲骸與位置](https://theriagames.com/guide/wuthering-waves-impermanence-heron/)
- 雪鬃狼，乘霄山，COST 3，凝夜白霜或輕雲出月。可用資料塢追蹤。[1.1 新增聲骸](https://wutheringwaves.gg/version-1-1-thaw-of-eons/)、[地圖資料](https://wuthering.gg/map)
- 無妄者，北落野的無冠巨像之心週本，COST 4，沉日劫明。前往副本吸收聲骸。[聲骸資料](https://zilliongamer.com/wuthering-waves/c/echeos/dreamless)、[副本位置](https://primagames.com/gaming/how-to-get-and-use-dreamless-echo-skill-and-what-it-does-in-wuthering-waves)
- 千歿沉島夢魘聚落，隱海實驗場，COST 1／3，失序彼岸之夢。可在遊戲指南追蹤夢魘祓除。[官方 2.5 更新](https://wutheringwaves.kurogames.com/en/main/news/detail/2908)、[套裝與 COST 對照](https://wutheringwaves.fandom.com/wiki/Dream_of_the_Lost)

來源只涵蓋已核對的對應。若尚無指定套裝與 COST 的地點，規劃會列出原需求，並提示在遊戲資料塢核對與追蹤。遊戲內解鎖條件與當期掉落以遊戲顯示為準。

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
