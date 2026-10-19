# MAGIC-ERP

實驗器材與課程管理系統。把原本 6 張人工維護的 Excel（材料預算、庫存清點、庫存結餘、產出清點、共用器材、教室取物）整合成一套網頁系統：主檔只登記一次，庫存由異動自動計算，需求、採購 / 加工建議、每堂領料單全部自動產生。

設計說明（新舊表單對照、角色、流程、資料模型、計算公式）見 [docs/系統設計.md](docs/系統設計.md)。

## 功能

| 模組 | 取代 | 負責角色 |
|---|---|---|
| 物料主檔（耗材 / 非耗材 / 共用器材、加工品設定、CSV 匯入） | 各表重複的品名規格 | 課程設計師、倉儲 |
| 課程用料（課程 → 堂次 → 每堂用料、預算試算） | 2表 材料預算表 | 課程設計師 |
| 庫存（入庫、報廢、異動帳） | — | 倉儲 |
| 盤點（凍結帳面 → 實盤 → 過帳調整） | 3表 庫存清點表 | 倉儲 |
| 需求分析（自動計算缺料、建議採購 / 加工，可匯出 CSV） | 4表 庫存結餘表 | 倉儲 |
| 加工單（待加工 → 加工中 → 待確認 → 入庫） | 5表 產出清點表 | 倉儲 |
| 教室器材（標準配置、現場盤點、倉庫撥補） | 6表 共用器材表 | 授課人員、倉儲 |
| 開班排課 + 領料單（自動計算、列印、發料、課後歸還） | 7表 教室取物數量表 | 行政、授課人員 |

## 安裝與執行

需要 Python 3.10 以上。

```bash
pip install -r requirements.txt
flask --app magic_erp seed        # 建立範例資料（帳號 admin / designer / warehouse / teacher / office，密碼皆為 1234）
flask --app magic_erp run         # 開啟 http://127.0.0.1:5000
```

正式使用時不要匯入範例資料，改為建立管理員帳號：

```bash
flask --app magic_erp create-user admin admin --name 管理員
```

環境變數：

- `MAGIC_ERP_DB`：SQLite 檔案位置（預設 `instance/magic_erp.sqlite3`）
- `MAGIC_ERP_SECRET`：Session 加密金鑰，正式環境務必設定

## 測試

```bash
pip install pytest
python -m pytest
```

## 專案結構

```
magic_erp/
  schema.sql     資料表定義
  services.py    業務邏輯（用量計算、需求分析、領料、加工、盤點、器材）
  views.py       網頁路由
  auth.py        登入與角色權限
  seed.py        範例資料
  templates/     頁面
docs/系統設計.md  系統設計文件
tests/           自動化測試
```
