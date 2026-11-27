# 照片歸檔系統跨業務整合指南（第一階段：API、MCP 與照片挑選器）

本文件說明照片歸檔系統第一階段跨業務整合的架構、資料遷移、設定方式、API／MCP 介面、照片挑選器嵌入方式與已知限制。

## 1. 架構總覽

所有整合通道（REST API `/integration/v1/*`、HTTP MCP `/mcp`、stdio MCP `manage.py mcp_server`、照片挑選器 `/picker`）皆呼叫同一共用授權與服務層：

- `core/integration_auth.py`：雙層授權交集（串接系統 ∩ 操作人員）、OAuth／OIDC 憑證驗證、短效簽章圖片憑證、頻率限制與結構化稽核。
- `core/integration_service.py`：共用資料集查詢、一般／附近／OCR／中文影像內容搜尋、批次最新狀態解析、報告用圖副本產生（EXIF 方向校正、不放大、剝除未授權 EXIF）與冪等報告匯入。
- `core/mcp_server.py`：將共用服務包裝為四個唯讀 MCP 工具（`list_photo_datasets`、`search_photos`、`get_photo_previews`、`get_photo_details`）。
- `core/views_integration.py`：HTTP 端點、OAuth 2.0（含 PKCE S256 與經驗證身分交換）、MCP HTTP 端點、挑選器工作階段與後台管理 API。
- `templates/picker.html` & `static/picker.js`：可重用照片挑選器（iframe／彈出視窗）。
- `templates/integration_demo.html` & `static/integration-demo.js`：防災系統整合示範工作台（`/integration-demo`），端到端展示「AI 搜尋回圖」與「勘災報告選圖匯入」。

## 2. 資料遷移與設定

```powershell
# 1. 套用資料庫遷移（0016_integrationsystem_...）
python manage.py migrate

# 2. 系統檢查
python manage.py check

# 3. 執行第一階段驗收測試（A01-A20 + OAuth + 萬筆分頁）
python manage.py test core.test_integration --verbosity=2
```

### 管理後台設定

使用系統管理者帳號（如 `admin`）登入主畫面 →「管理後台 → 串接系統管理」：

1. 建立串接系統（例如 `disaster-response`／防災系統），明確勾選授權資料集、功能權限、每次回傳上限、匯入上限與允許來源網域（如 `http://127.0.0.1:8000` 或 `https://disaster.agency.gov.tw`），並填寫理由。
2. 系統密鑰（`client_secret`）僅於建立或輪換時顯示一次，須由業務系統後端妥善保管，不可放入前端、URL 或 Git。
3. 透過 OAuth 2.0 授權碼＋PKCE（`/integration/oauth/authorize` → `/integration/oauth/token`）或經驗證的外部身分綁定（`ExternalIdentityBinding` + `grant_type=identity_exchange`）取得人員綁定的短效 Bearer 憑證；本機驗證亦可由管理者於後台核發人員測試憑證。

## 3. 整合 API 端點（`/integration/v1/`）

| 端點 | 方法 | 用途 |
|---|---|---|
| `/integration/v1/capabilities` | `GET` | 查詢目前雙層身分可用功能與限制 |
| `/integration/v1/datasets` | `GET` | 列出授權交集內的資料集與允許自訂欄位 |
| `/integration/v1/photos/search` | `POST` | 分頁搜尋（`general` / `nearby` / `ocr` / `semantic`），預設時區 `Asia/Taipei` |
| `/integration/v1/photos/resolve` | `POST` | 批次驗證照片最新授權狀態與版本變更 |
| `/integration/v1/photos/{id}/preview-ticket` | `POST` | 取得短效、綁定系統／人員／照片／用途的預覽憑證 |
| `/integration/v1/report-imports` | `POST` | 建立冪等報告取圖作業（`idempotency_key`），產出已剝除 EXIF 的報告副本 |
| `/integration/v1/report-imports/{id}` | `GET` | 查詢匯入作業與逐張狀態（即時重新驗權） |
| `/integration/v1/report-imports/{id}/retry` | `POST` | 安全重試 `partial_failed` 作業中的失敗項目 |
| `/integration/v1/assets/{ticket}` | `GET` | 受控下載預覽、報告用圖或已授權原圖（附 `X-Content-SHA256`） |
| `/integration/v1/picker-sessions` | `POST` | 建立短效照片挑選器工作階段（驗證允許來源網域） |

### OAuth / OIDC 與 MCP 探索端點

- `GET /.well-known/oauth-protected-resource`
- `GET /.well-known/oauth-authorization-server`
- `POST /integration/oauth/authorize`
- `POST /integration/oauth/token`
- `POST /integration/oauth/revoke`

## 4. MCP 工具介面

- **HTTP MCP**：`POST /mcp`（須附 `Authorization: Bearer <token>`；未授權時依官方規範回傳 `401` 與 `WWW-Authenticate: Bearer resource_metadata="..."`）。
- **CLI stdio MCP**：`PHOTO_MCP_TOKEN=<token> python manage.py mcp_server`。

第一階段僅開放四個唯讀工具：

1. `list_photo_datasets`
2. `search_photos`
3. `get_photo_previews`（回傳實際 MCP `image` Base64 JPEG 內容區塊與結構化中繼資料）
4. `get_photo_details`

所有工具結果皆附帶未受信任資料提醒（`_untrusted_data_notice`），且不提供刪除、修改、搬移或繞過人工確認的報告匯入工具。

## 5. 已知限制（第一階段本機版）

1. **資料庫與空間索引**：本機版以 SQLite 實作萬筆分頁與邊界框過濾；正式百萬筆以上大規模環境仍須搭配 PostgreSQL／PostGIS 與獨立快取（如 Redis）做分散式限流。
2. **機關 SSO／OIDC 實接**：目前提供標準 OAuth 2.0 授權碼＋PKCE 及 `ExternalIdentityBinding`（`issuer` + `subject`）身分交換介面，實際機關 IdP（如 Keycloak／ADFS／政府單一簽入）的 JWKS 簽章驗證須於正式環境依機關規格對接。
3. **防災系統端整合**：本專案內附 `/integration-demo` 作為端到端驗證工作台；防災系統正式版須依本規格在其後端保管 `client_secret`、驗證圖片 SHA-256 並以「匯入作業＋照片項目識別」防止重複匯入。
4. **圖片檔案生命週期**：已產出的報告用圖副本位於 `storage/integration_reports/`，正式環境須另訂清理排程與保留期限。
