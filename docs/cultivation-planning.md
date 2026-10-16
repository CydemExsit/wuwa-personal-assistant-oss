# 養成佇列與每日規劃

Milestone 7 把角色完成度、人工維護的素材需求與本機庫存轉成可解釋的帳號養成順序。
規劃器完全離線，不讀取遊戲、帳號 API、log 或第三方網站，也不會在背景自行同步資料。

## 資料流與責任邊界

1. `CharacterCultivationService` 提供常用角色、武器、技能、節點與五個永久聲骸的快照。
2. `CultivationPlanningService` 以 SQLite transaction 管理素材、庫存、養成目標及每日規劃快照。
3. `wuwa_assistant.planning` 僅接收 immutable domain input；它不依賴 Qt、SQLAlchemy 或目前時間，
   因此同一組輸入會得到相同排序與日期估計。
4. `TodayPlanPresenter` 在背景執行資料庫查詢與規劃，把 immutable view model 送回 Qt UI thread。
5. Dashboard 只接收今日第一順位的摘要；角色進度、今日規劃與待辦卡片各自更新，避免非同步
   完成順序互相覆蓋。

## Breadth-first 排序

V1 以可測試、可說明的優先群組排序，而不是把所有工作壓成一個不透明分數。由高到低為：

1. 套裝或主詞條經使用者確認為錯誤。
2. 常用角色尚未裝滿五顆永久聲骸。
3. 角色的關鍵功能門檻尚未達成。
4. 已裝備聲骸中分數最低且未及格的位置。
5. 核心技能、武器、小型屬性節點或其他明確目標未完成。
6. 已達及格的角色降低優先度；所有常用角色及格後才進入第二輪最佳化。

同一群組內依使用者角色優先級、完成度、到期日及穩定識別碼排序。未知資料不會被當成已完成，
但也不會被猜成錯誤；畫面會標示需要人工確認。

## 素材缺口與每日波片

素材缺口為 `max(需求量 - 本機庫存, 0)`。規劃器只在素材具備已知的單次波片成本、平均產出，
且當日屬於開放日時建議刷取次數。若平均產出未知、開放日資料不足或庫存尚未輸入，則完成日
保持空值並提示人工確認。每週限次會限制本週可排的次數，不會把超出部分假裝成今日可完成。

估計完成日是以目前人工輸入的每日可用波片與素材規則推算的規劃值，不代表官方保證掉落。
平均產出來源狀態與版本會跟素材定義一起保存；手動或 fallback 資料會清楚標示。

## 靜態資料匯入

V1 只接受使用者主動選擇的本機 JSON 更新包。匯入流程會先解析、驗證 schema/version、顯示
預覽，再以單一 transaction 套用；未知 schema、同一資料集內重複 code 或非法數值都會拒絕，
不會留下部分更新。正式資料與示範資料使用不同 dataset，庫存與目標不會互相混合。

Schema v1 範例：

```json
{
  "schema_version": 1,
  "source_version": "manual-2026-08",
  "materials": [
    {
      "code": "example-skill-material",
      "name": "範例技能素材",
      "category": "技能素材",
      "stamina_cost_per_run": 40,
      "average_yield": 3.0,
      "open_days": [1, 3, 5],
      "weekly_limit": null,
      "notes": "手動資料，僅供示範"
    }
  ]
}
```

`open_days` 使用 ISO weekday（1 為星期一，7 為星期日）。`average_yield` 是規劃用平均值，
不是官方保證；匯入後會以 `source_status=manual` 與 `source_version` 保存來源狀態。

素材 `code` 只在所屬 dataset 中唯一；正式資料與示範資料可以使用相同 code，但會建立不同的
素材 ID，彼此的定義、庫存、目標及每日規劃不會混用。Migration 0003 會保留既有素材關聯並把
舊的全資料庫唯一限制安全地改成 dataset-scoped unique constraint。

## 每日規劃快照

每次重新規劃會建立新的 generation ID，舊 generation 保留為歷史或明確標記為封存。規劃列
保存目標、素材、順位、建議次數、波片成本、理由、估計完成日與使用過的假設，確保重啟後仍
能解釋當時為何給出該建議。

使用者可手動把當日 generation 標記為已執行。規劃器會把已完成（或之後被新 generation
封存但原狀態為已完成）的刷取次數計入當週限次；實際掉落量不會由平均值自動寫入庫存，畫面
會要求使用者再填入真正取得的數量，避免把估計產出當成帳號真值。
