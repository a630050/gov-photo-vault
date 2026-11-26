# 本機部署與維運手冊

本階段提供 Windows 可實際執行的部署版本，沿用目前 Python／Django 與 SQLite。照片可放 UNC 網路芳鄰，SQLite、執行檔、模型、密鑰與監督目錄放本機磁碟。正式技術方案的 PostgreSQL／PostGIS、機關登入整合、HTTPS 反向代理及設備層備援仍須另行部署驗收；此版本不能直接視為全面正式上線。

## 架構與邊界

```text
Windows 排程（登入／開機）
  └─ ops.py run：單一監督程序、輪替記錄、每日已驗證備份
      ├─ Waitress → 127.0.0.1:8010 → Django 核心
      ├─ semantic_index（設定啟用後）→ 本機離線模型
      └─ external_bridge（設定啟用後）→ 獨立接收閘道
核心：本機 SQLite + 指定照片儲存目錄／UNC
閘道：獨立 profile、SQLite、暫存照片、排程與備份
```

各服務只監聽 loopback；不會自動開防火牆或對外發布。外部接收閘道保持獨立設定與資料庫，載入 `gateway_ops_settings` 不會載入 `core` app。正式 DMZ 閘道應在另一台主機執行。Waitress 使用方式依[官方文件](https://docs.pylonsproject.org/projects/waitress/en/stable/usage.html)。

## 建立獨立部署設定

在專案根目錄執行，使用已安裝的 Python 與 `.vendor`；若缺套件，依 `requirements-local.txt` 安裝到 `.vendor`。

```powershell
python ops.py prepare --profile .runtime/deployment/profile.json --directory .runtime/deployment --port 8010
python ops.py migrate --profile .runtime/deployment/profile.json
$env:PHOTO_OPS_CONFIG=(Resolve-Path .runtime/deployment/profile.json).Path
$env:DJANGO_SETTINGS_MODULE='ops_settings'
python manage.py createsuperuser
python ops.py run --profile .runtime/deployment/profile.json
```

另開終端執行 `python ops.py status --profile .runtime/deployment/profile.json`，瀏覽 `http://127.0.0.1:8010/`。部署流程不會建立預設帳密或測試資料；`seed_demo` 僅限獨立測試環境。要回到既有開發版，移除上述兩個環境變數再使用 `start-local.ps1`。

`prepare` 只接受新的設定檔、資料庫、密鑰與空白照片目錄；不移動既有 `data.sqlite3`、`storage/` 或使用者資料。若指定既有照片儲存區，必須使用其原設定檔或依後面的停機移轉流程處理。JSON 路徑全部為絕對路徑，不要共用不同部署的 run 目錄。

可調整 profile：`port`、`min_free_mb`（預設 512）、`backup_hours`（預設 24，最少 1 小時）、`backups`、`semantic_worker`、`bridge_worker`、`allowed_hosts`。變更後先停止再啟動。啟用 bridge 時，加入 `gateway_config`，填入先前 `prepare_external` 產生的外部接收設定檔絕對路徑；不可將橋接金鑰放入前端或記錄。模型環境與權重沿用專案本機檔案，不從網路下載。

密鑰保存在部署目錄的 `secret.txt`；設定與密鑰不進 Git 或照片備份。部署人員應以 NTFS ACL 限制專用執行帳號及維運管理者讀取，將密鑰與橋接設定另存於機關核准的密鑰備份位置。照片備份本身含原圖、人員參考照片與資料庫，亦須限制 ACL 並按機關規範加密保存。

## 網路芳鄰儲存

先由檔案伺服器管理員建立空目錄，例如 `\\NAS01\AgencyPhotos\production`，再以將來執行服務的帳號操作：

```powershell
python ops.py prepare --profile .runtime/production/profile.json --directory .runtime/production --media '\\NAS01\AgencyPhotos\production' --port 8010
python ops.py migrate --profile .runtime/production/profile.json
python ops.py status --profile .runtime/production/profile.json
```

使用 UNC，避免登入者的映射磁碟。帳號需有分享及 NTFS 的列目錄、讀取、新增、修改、重新命名與刪除測試檔權限，供縮圖、草稿及健康探測使用；應用程式的人員刪除仍走申請審核。不要把 SMB 密碼放 JSON、指令列或程式碼。使用機關服務帳號的 Windows 網路認證；不可用 LocalSystem 代替已驗證的網域帳號。SQLite 拒絕 UNC 及映射網路磁碟。

儲存區有 `.photo-storage-id` 識別檔，不能刪除或隨意重建。每次應用存取先確認識別與剩餘空間；缺少儲存區或識別不符時回應 503，不改存其他目錄。readiness 會執行建立／寫入／同步／讀回／改名／刪除小型探測檔。SMB 斷線回應時間仍受 Windows 網路逾時影響。

既有開發資料轉部署：停止開發網頁、內容索引與橋接程序；建立及驗證完整備份；還原到新目錄，保留來源。建立新的部署 profile 後停止其服務，將還原的資料庫與照片完整複製到新部署位置，確認照片根目錄的識別檔與 profile `storage_id` 一致，再執行 migrate、status、登入及原圖抽查。若來源舊版備份沒有識別檔，只能在已確認為正確的完整目的儲存區建立一次識別檔。切換前確認所有寫入者皆已停機；不可讓開發版與部署版同時寫同一資料庫。

## 服務自動啟動與復原

```powershell
# 只產出 service-task.xml，不修改 Windows 排程
.\install-service-task.ps1 -Profile .runtime/deployment/profile.json
# 部署人員檢閱後安裝「登入時啟動」
.\install-service-task.ps1 -Profile .runtime/deployment/profile.json -TaskName AgencyPhotoSystem -Install
# 正式服務帳號：以該帳號、具註冊權限的終端安裝開機排程；互動輸入認證
.\install-service-task.ps1 -Profile .runtime/deployment/profile.json -TaskName AgencyPhotoSystem -Trigger Startup -Install
```

登入排程僅在帳號登入後運作；無人值守選 Startup，需具「以批次工作登入」及實際 SMB 權限。此處採 Windows 工作排程，不冒充 SCM 原生 Windows Service。腳本拒絕覆寫同名工作，預覽不需排程管理權限，安裝才呼叫 Windows 註冊 API；沒有修改作業系統的防火牆或自動登入設定。

監督程序以獨立鎖阻止重複啟動；網頁、索引、橋接程序意外結束後，從 1 秒到最多 60 秒退避重新啟動。監督程序本身由排程設定失敗重啟，最多 3 次，仍失敗需人工處理。`run/supervisor.log` 每份最多 2 MiB、保留 5 份輪替；模型本身記錄沿用 `.runtime/*worker.log`，需依機關保存政策納入輪替管理。工作程序另寫入批次心跳，超過 180 秒標示逾時；待命、處理中或錯誤狀態可在後台查看。程序存活不等於模型品質或工作完成，應搭配索引狀態及記錄檢查。

```powershell
python ops.py stop --profile .runtime/deployment/profile.json
python ops.py status --profile .runtime/deployment/profile.json
```

stop 以該 profile 的停止檔通知監督與工作程序；最多等待 35 秒，仍未退出才終止自己建立的子程序。應先停止接受新上傳，再停服務。若要永久停用排程，先 `Disable-ScheduledTask -TaskName AgencyPhotoSystem`，再使用 stop。`run` 會清除停止訊號，因此不要在排程仍啟用時當作永久停用。

## 備份、驗證與還原

```powershell
python ops.py backup --profile .runtime/deployment/profile.json
python ops.py verify --archive 'D:\Backup\photo-core-....zip'
python ops.py restore --archive 'D:\Backup\photo-core-....zip' --destination 'D:\Recovery\photo-20261124'
```

部署版的 HTTP 操作、索引與橋接批次持共享鎖；快照持獨占鎖，等待進行中的寫入完成。備份使用 SQLite backup API，不複製使用中的 SQLite 檔案；封裝本次資料庫及完整照片目錄（原圖、縮圖、人物參考照、匯出包、回收照片），驗證每檔 SHA-256／大小、SQLite integrity／外鍵及資料庫所引用的實體檔案。資料庫引用檔案缺漏、目錄不可讀或 reparse point 時直接失敗；成功驗證後才將 `.partial` 改名 ZIP。中途失敗不取代最近成功備份紀錄。

只有遵循同一 profile 鎖的寫入者才受協調保護。直接管理指令、檔案總管修改、其他主機寫入照片以及舊版 dev server 不受此鎖保護，備份時必須停止。單機 SQLite 版本不可部署多台寫入節點。

監督啟動時若尚無成功備份會立即建立，之後按 `backup_hours` 排程。後台標示最近一次備份失敗，以及超過排程時間加 5 分鐘仍無成功備份的逾期狀態。失敗會記錄並約 5 分鐘後重試。備份預設放本機部署目錄，需將 `backups` 改到獨立受控磁碟／備份分享，另做離線／異地副本；放同機不能防整機損毀。程式不自動刪除歷史備份，避免誤刪；維運需訂保留期間、容量告警與核准的清理作業。不要在照片儲存目錄內放備份、資料庫、執行期檔案或密鑰。

還原前完整驗證 ZIP，拒絕路徑穿越、重複項目、非預期檔案及雜湊錯誤；只允許輸出到不存在的新目錄。還原先寫暫存目錄、成功後才切換成指定名稱，不覆寫正式資料。輸出有 `data.sqlite3`、`storage/` 與 `restore-manifest.json`。資料正確性抽查完成後，停機切換 profile 路徑；還原版要使用獨立 run／連接埠，避免同時使用相同排程或停止檔。

版本 2 備份不接受舊版缺少資料庫雜湊的 manifest；舊 ZIP 保留，請先在來源停機後用新版 `backup_local` 重建。開發版仍可用 `python manage.py backup_local`，但沒有部署鎖，必須先停止開發網頁與所有工作程序。

接收閘道另外備份：

```powershell
python ops.py prepare --profile .runtime/gateway-production/profile.json --directory .runtime/gateway-production --component gateway --gateway-config .runtime/external-config.json --port 8012
python ops.py migrate --profile .runtime/gateway-production/profile.json
python ops.py run --profile .runtime/gateway-production/profile.json
python ops.py backup --profile .runtime/gateway-production/profile.json
```

核心與閘道不是跨主機原子快照；災難復原須先停止橋接、先還原核心並驗證，再還原閘道，最後檢查接收收據及待傳送項目後恢復橋接。金鑰設定須從獨立密鑰備份復原。不可將另一個時點的核心／閘道任意混用後直接對外開放。

## 健康檢查與日常作業

| 入口 | 可見內容 | 判讀 |
|---|---|---|
| `/health/live` | 匿名，只有 alive | HTTP 程序能回應 |
| `/health/ready` | 匿名，只有 ready／unavailable | DB 遷移表、儲存識別、讀寫與空間；200／503 |
| `/ops/status` | 核心登入的系統管理者限定 | 詳細路徑、空間、監督心跳、最近成功備份 |
| 後台 → 部署與維運 | 系統管理者限定 | 同上，可手動重新檢查 |
| `ops.py status` | 作業系統維運帳號 | JSON；正常 exit 0，不就緒 exit 2 |

備份取得獨占鎖時，readiness 可能暫回 503；HTTP 寫入等待 10 秒仍無鎖才回 503，未拿到鎖不執行上傳／修改。健康入口不顯示密鑰、人名或照片資訊；詳細診斷不提供給一般同仁與匿名訪客。監督心跳每秒更新，超過 30 秒標示逾時；停止後明確顯示已停止。CLI readiness 表示資料庫及儲存可用，不能代替遠端 HTTP 探測，監控應另外檢查 live／ready、監督心跳與最新備份時間。

每日檢查服務狀態、最近成功備份時間、磁碟容量與錯誤記錄；每月在隔離路徑執行還原演練並抽查登入、科室權限、回收照片、人物關聯及原圖。外部監控／告警系統串接、真實 NAS 掉線與帳號撤權、實際重開機排程、跨設備復原、備份加密與 RPO／RTO 時間門檻皆須在部署環境另驗，不以本機演練替代。

## 可重跑的本機驗證

```powershell
python manage.py test core.test_operations
python verify-operations.py
```

整合演練會建立 `.runtime/ops-drill-隨機值/`，使用臨時 loopback 連接埠、新資料庫與程式產生的測試照片，不使用既有帳號或照片。驗證 Waitress、殺掉演練自己的網頁子程序後自動重啟、暫時移開演練自己的照片目錄後 live 200／ready 503／操作 503、不自動建立替代目錄、備份驗證、全新目錄還原及照片內容一致；結束一定要求停止自己的服務，保留備份、還原與 `drill-result.json` 供查核。

本次實測：核心全套 116 項（新增補測後維運共 10 項）、獨立閘道 12 項；整合演練結果見 `.runtime/ops-drill-22f9243eaa/drill-result.json`。Windows 排程 XML 已本機產生並解析，未註冊永久排程，未宣稱已實際重開機或連接真實 NAS。


## 統一背景工作行程（`job_worker`）

第二批次起，`ops.py prepare` 預設啟用 `job_worker: true`（既有 profile 亦可於 JSON 加入 `"job_worker": true`），由 `ops.py run` 自動監督啟動 `job_worker`：

```powershell
# 單次處理目前佇列（開發／排錯）
python manage.py job_worker --once

# 持續輪詢待處理工作（支援租約逾時接手與 --stop-file 安全停止）
python manage.py job_worker --watch

# 透過 ops.py 單獨啟動工作行程
python ops.py worker job_worker --profile .runtime/deployment/profile.json
```

工作程序每處理一個安全批次（匯出 50 張、近似分析 10 張、行政區重算 50 張、內容索引 5 張）即更新 `lease_until`（60 秒）並檢查 `cancel_requested` 與執行者即時權限。若行程意外終止，新 worker 會在租約到期後自動接手未完成工作，且重試不重複建立 `Export` 或 `SimilarityPair` 紀錄。


## 行政區邊界管理與檔案對帳維運

- **行政區邊界版本**：系統管理者可於「管理後台 → 部署與維運」查看目前啟用的 `AdminBoundaryVersion` 校驗碼與來源授權，透過 `POST /api/boundary/save` 註冊新版本並依需要啟用，或排程 `region_recompute` 背景工作依指定版本重算行政區（重算期間若照片座標被更正，舊工作結果不會覆寫新座標）。
- **檔案對帳與容量**：`reconcile/run`（或工作中心 `kind='reconcile'`）預設為**只產報告、不刪除任何檔案**的唯讀對帳，並套用寬限期排除進行中上傳／暫存與執行中背景工作。縮圖修復（`reconcile/repair_previews`）只重算缺漏縮圖，不變動原圖與孤立檔；孤立檔清理須透過 `reconcile/confirm_orphan_cleanup` 由管理者填寫理由並二次核對資料庫引用與寬限期後執行。


## 2026-11-26 修正後的驗證與清理限制

本次修補搜尋與選取範圍綁定、歷史欄位條件保留、草稿切換、舊群聚失效、孤立檔清理、撤權稽核與多模型重建重試。沒有資料庫結構變更，無須新增遷移。後端及 worker 須在重啟後載入修正，前端須重新整理。

孤立檔報告現在保存檔案大小、奈秒修改時間、裝置／檔案識別與 SHA-256。舊報告若缺少完整識別，清理會跳過並要求重新對帳。清理採 SQLite 寫入交易協調引用更新；逐檔重新驗證後，先將物件移入 `.cleanup-hold` 再檢查引用及身分，驗證失敗會復原而不刪除。若原位置被其他檔案占用，原檔保留在 `.cleanup-hold`，回應及稽核會記錄 `retained_path`，應由維運人員核對後復原，不可直接清空該目錄。此協調針對目前單機 SQLite 版本；正式更換資料庫或多節點部署須另驗檔案寫入協調，外部程式不得繞過系統直接更改照片或資料庫。

背景與同步對帳每批最多 100 個項目檢查操作者權限，產生報告前再確認；停用帳號造成的中止亦保留操作者稽核。多模型重建分別保存各模型準備狀態，支援接手舊版部分完成工作的游標。

可重跑的隔離驗證：

```powershell
# 所有核心測試使用記憶體 DB 與臨時 MEDIA_ROOT；--fast 只加速測試帳號雜湊
python -B scripts/test_isolated.py --fast
python -B scripts/test_isolated.py --gateway --fast
# 10 項新增後端競態／重試測試，以及 6 項前端延遲與失敗時序測試
python -B scripts/test_isolated.py core.test_regressions --fast
npm run test:frontend
```

本次核心 158 項、接收閘道 12 項及前端 6 項均通過。前端測試直接執行正式函式並注入延遲與失敗回應；本次瀏覽器自動化工具初始化失敗，因此未完成瀏覽器畫面操作驗證。未測試真實 NAS 斷線或多主機並行，不作已驗收宣稱。
