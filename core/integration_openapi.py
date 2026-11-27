"""OpenAPI 3.1.0 specification generator for Integration API v1, OAuth/OIDC, and HTTP MCP."""
from .integration_auth import INTEGRATION_SCOPES


def build_integration_openapi_spec(base_url: str) -> dict:
    base = str(base_url or '').rstrip('/')
    error_ref = {'$ref': '#/components/schemas/ErrorResponse'}

    return {
        'openapi': '3.1.0',
        'info': {
            'title': '照片歸檔系統跨業務整合 API（Integration API v1 & MCP）',
            'version': '1.1.0',
            'description': (
                '提供其他業務系統（如防災系統）與 AI Agent 經雙層授權交集'
                '（串接系統 ∩ 操作人員 ∩ 本次請求範圍）查詢授權資料集、搜尋正式照片、'
                '取得受控短效預覽／報告用圖與建立冪等報告匯入作業。\n\n'
                '- 預設業務時區：`Asia/Taipei`（日期迄日包含完整當天）。\n'
                '- 預設僅提供正式照片（`submitted`），不包含草稿與回收照片。\n'
                '- 所有取圖均須二次驗權，短效憑證不可跨系統／跨照片／升級成原圖使用。'
            ),
        },
        'servers': [{'url': base or 'http://127.0.0.1:8000', 'description': '目前照片歸檔系統實例'}],
        'tags': [
            {'name': 'OAuth / Discovery', 'description': 'OAuth 2.0（含 PKCE S256）、外部身分交換、撤銷與 MCP 探索中繼資料'},
            {'name': 'Integration API v1', 'description': '跨業務整合核心資料集、搜尋、解析、預覽憑證與報告匯入介面'},
            {'name': 'Photo Picker', 'description': '可嵌入 iframe／彈出視窗之短效挑選器工作階段'},
            {'name': 'MCP', 'description': 'Model Context Protocol JSON-RPC 2.0（HTTP Streamable）唯讀工具介面'},
        ],
        'components': {
            'securitySchemes': {
                'BearerAuth': {
                    'type': 'http',
                    'scheme': 'bearer',
                    'bearerFormat': 'OpaqueToken',
                    'description': '透過 /integration/oauth/token 核發的短效存取憑證',
                },
            },
            'schemas': {
                'ErrorResponse': {
                    'type': 'object',
                    'required': ['error'],
                    'properties': {
                        'error': {
                            'type': 'object',
                            'required': ['code', 'message'],
                            'properties': {
                                'code': {
                                    'type': 'string',
                                    'enum': [
                                        'UNAUTHENTICATED',
                                        'PERMISSION_DENIED',
                                        'INVALID_CRITERIA',
                                        'AMBIGUOUS_CRITERIA',
                                        'NOT_FOUND',
                                        'VERSION_CHANGED',
                                        'INDEX_INCOMPLETE',
                                        'MODEL_UNAVAILABLE',
                                        'STORAGE_UNAVAILABLE',
                                        'FILE_EXPIRED',
                                        'RATE_LIMITED',
                                        'LIMIT_EXCEEDED',
                                    ],
                                },
                                'message': {'type': 'string'},
                                'details': {'type': 'object', 'additionalProperties': True},
                            },
                        },
                    },
                },
                'SearchRequest': {
                    'type': 'object',
                    'properties': {
                        'mode': {
                            'type': 'string',
                            'enum': ['general', 'nearby', 'ocr', 'semantic'],
                            'default': 'general',
                        },
                        'datasets': {
                            'type': 'array',
                            'items': {'type': 'integer'},
                            'description': '欲查詢的授權資料集 ID（省略時為有效授權交集全部資料集）',
                        },
                        'from': {'type': 'string', 'description': '拍攝起日（YYYY-MM-DD 或含時區 ISO 8601）'},
                        'to': {'type': 'string', 'description': '拍攝迄日（YYYY-MM-DD 含當天，或含時區 ISO 8601）'},
                        'nearby': {
                            'type': 'object',
                            'required': ['lat', 'lon'],
                            'properties': {
                                'lat': {'type': 'number'},
                                'lon': {'type': 'number'},
                                'radius_m': {'type': 'number', 'default': 500},
                            },
                        },
                        'bounds': {
                            'type': 'object',
                            'required': ['north', 'south', 'east', 'west'],
                            'properties': {
                                'north': {'type': 'number'},
                                'south': {'type': 'number'},
                                'east': {'type': 'number'},
                                'west': {'type': 'number'},
                            },
                        },
                        'county': {'type': 'string'},
                        'town': {'type': 'string'},
                        'village': {'type': 'string'},
                        'tags': {'type': 'array', 'items': {'type': 'string'}},
                        'fields': {'type': 'array', 'items': {'type': 'object'}},
                        'text': {'type': 'string', 'description': '檔名或中繼資料關鍵字'},
                        'query': {'type': 'string', 'description': 'OCR 文字或中文影像內容查詢字串'},
                        'minimum': {'type': 'number', 'description': '影像內容搜尋最低餘弦相近度（預設 0.20）'},
                        'limit': {'type': 'integer', 'default': 20, 'minimum': 1, 'maximum': 200},
                        'offset': {'type': 'integer', 'default': 0, 'minimum': 0},
                    },
                },
                'ReportImportRequest': {
                    'type': 'object',
                    'required': ['idempotency_key', 'items'],
                    'properties': {
                        'idempotency_key': {'type': 'string', 'maxLength': 128},
                        'case_id': {'type': 'string', 'maxLength': 120},
                        'report_id': {'type': 'string', 'maxLength': 120},
                        'items': {
                            'type': 'array',
                            'minItems': 1,
                            'items': {
                                'type': 'object',
                                'required': ['photo_id'],
                                'properties': {
                                    'photo_id': {'type': 'integer'},
                                    'expected_version': {'type': 'integer'},
                                    'ordinal': {'type': 'integer'},
                                    'caption': {'type': 'string'},
                                    'variant': {'type': 'string', 'enum': ['report', 'original'], 'default': 'report'},
                                },
                            },
                        },
                    },
                },
                'PickerSessionRequest': {
                    'type': 'object',
                    'required': ['origin'],
                    'properties': {
                        'origin': {'type': 'string', 'description': '嵌入頁來源網域（須在允許清單內）'},
                        'case_id': {'type': 'string'},
                        'report_id': {'type': 'string'},
                        'callback_url': {'type': 'string'},
                        'max_select': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                        'defaults': {'type': 'object', 'additionalProperties': True},
                    },
                },
            },
        },
        'paths': {
            '/.well-known/oauth-protected-resource': {
                'get': {
                    'tags': ['OAuth / Discovery'],
                    'summary': 'MCP OAuth Protected Resource 中繼資料',
                    'responses': {'200': {'description': 'RFC 9728 受保護資源中繼資料'}},
                },
            },
            '/.well-known/oauth-authorization-server': {
                'get': {
                    'tags': ['OAuth / Discovery'],
                    'summary': 'OAuth 2.0 Authorization Server 中繼資料',
                    'responses': {'200': {'description': 'RFC 8414 授權伺服器中繼資料'}},
                },
            },
            '/integration/oauth/authorize': {
                'post': {
                    'tags': ['OAuth / Discovery'],
                    'summary': '為已登入歸檔系統之人員核發短效授權碼（支援 PKCE S256）',
                    'responses': {
                        '200': {'description': '核發授權碼與有效秒數'},
                        '401': {'description': '尚未登入', 'content': {'application/json': {'schema': error_ref}}},
                        '403': {'description': '系統或 redirect_uri 未獲授權', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/oauth/token': {
                'post': {
                    'tags': ['OAuth / Discovery'],
                    'summary': '以 authorization_code (+PKCE)、identity_exchange 或 client_credentials 換發短效存取憑證',
                    'responses': {
                        '200': {'description': 'Bearer access_token 與有效期限／範圍'},
                        '401': {'description': '憑證或綁定驗證失敗', 'content': {'application/json': {'schema': error_ref}}},
                        '403': {'description': '未授權的 grant 或 scope', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/oauth/revoke': {
                'post': {
                    'tags': ['OAuth / Discovery'],
                    'summary': '即時撤銷既有存取憑證',
                    'responses': {'200': {'description': '已撤銷'}},
                },
            },
            '/integration/v1/openapi.json': {
                'get': {
                    'tags': ['Integration API v1'],
                    'summary': '取得 OpenAPI 3.1.0 JSON 規格',
                    'responses': {'200': {'description': 'OpenAPI 3.1.0 文件'}},
                },
            },
            '/integration/v1/capabilities': {
                'get': {
                    'tags': ['Integration API v1'],
                    'summary': '查詢目前雙層身分可用功能、權限範圍與使用限制',
                    'security': [{'BearerAuth': []}],
                    'responses': {
                        '200': {'description': '目前身分能力與上限'},
                        '401': {'description': '未登入或憑證過期', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/v1/datasets': {
                'get': {
                    'tags': ['Integration API v1'],
                    'summary': '列出授權交集內的資料集與允許提供的自訂欄位定義',
                    'security': [{'BearerAuth': []}],
                    'responses': {
                        '200': {'description': '授權資料集清單'},
                        '401': {'description': '未登入或憑證過期', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/v1/photos/search': {
                'post': {
                    'tags': ['Integration API v1'],
                    'summary': '依指定模式（general / nearby / ocr / semantic）分頁搜尋授權照片',
                    'security': [{'BearerAuth': []}],
                    'requestBody': {
                        'required': True,
                        'content': {'application/json': {'schema': {'$ref': '#/components/schemas/SearchRequest'}}},
                    },
                    'responses': {
                        '200': {'description': '分頁候選照片、正規化條件、search_id 與索引涵蓋狀態'},
                        '422': {'description': '搜尋條件不明或格式錯誤', 'content': {'application/json': {'schema': error_ref}}},
                        '503': {'description': 'OCR 或內容搜尋模型暫不可用', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/v1/photos/resolve': {
                'post': {
                    'tags': ['Integration API v1'],
                    'summary': '依照片 ID 批次取得最新授權狀態與版本檢查結果',
                    'security': [{'BearerAuth': []}],
                    'responses': {'200': {'description': '逐張最新狀態（available / version_changed / unavailable）'}},
                },
            },
            '/integration/v1/photos/{photo_id}/preview-ticket': {
                'post': {
                    'tags': ['Integration API v1'],
                    'summary': '取得短效、綁定系統／人員／照片／用途的預覽取圖憑證',
                    'security': [{'BearerAuth': []}],
                    'parameters': [
                        {'name': 'photo_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}},
                    ],
                    'responses': {
                        '200': {'description': '短效預覽憑證與取圖路徑'},
                        '403': {'description': '無權存取或照片已回收', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/v1/report-imports': {
                'post': {
                    'tags': ['Integration API v1'],
                    'summary': '確認選定照片並建立冪等報告取圖作業（產出剝除未授權 EXIF 之報告副本）',
                    'security': [{'BearerAuth': []}],
                    'requestBody': {
                        'required': True,
                        'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ReportImportRequest'}}},
                    },
                    'responses': {
                        '200': {'description': '冪等重播既有的報告匯入作業'},
                        '201': {'description': '已建立報告匯入作業並回傳逐張狀態與短效下載憑證'},
                        '422': {'description': '參數錯誤或超過上限', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/v1/report-imports/{job_id}': {
                'get': {
                    'tags': ['Integration API v1'],
                    'summary': '查詢報告匯入作業結果與逐張即時授權狀態',
                    'security': [{'BearerAuth': []}],
                    'parameters': [
                        {'name': 'job_id', 'in': 'path', 'required': True, 'schema': {'type': 'string', 'format': 'uuid'}},
                    ],
                    'responses': {
                        '200': {'description': '作業摘要與逐張狀態（ready / failed / revoked / expired）'},
                        '404': {'description': '找不到匯入作業', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/v1/report-imports/{job_id}/retry': {
                'post': {
                    'tags': ['Integration API v1'],
                    'summary': '安全重試 partial_failed / failed 作業中尚未成功的照片項目',
                    'security': [{'BearerAuth': []}],
                    'parameters': [
                        {'name': 'job_id', 'in': 'path', 'required': True, 'schema': {'type': 'string', 'format': 'uuid'}},
                    ],
                    'responses': {'200': {'description': '重試後的最新作業結果'}},
                },
            },
            '/integration/v1/assets/{ticket}': {
                'get': {
                    'tags': ['Integration API v1'],
                    'summary': '以短效憑證受控下載預覽、報告用圖或已授權原圖（附 X-Content-SHA256）',
                    'security': [{'BearerAuth': []}],
                    'parameters': [
                        {'name': 'ticket', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                    ],
                    'responses': {
                        '200': {'description': '受控圖片位元組串流，含 X-Content-SHA256 與 Cache-Control: private, no-store'},
                        '403': {'description': '憑證不符、過期、撤權或照片已回收', 'content': {'application/json': {'schema': error_ref}}},
                        '410': {'description': '報告用圖副本已逾保留期限並依政策清理', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/integration/v1/picker-sessions': {
                'post': {
                    'tags': ['Photo Picker'],
                    'summary': '建立短效照片挑選器工作階段（驗證允許來源網域並限定 CSP frame-ancestors）',
                    'security': [{'BearerAuth': []}],
                    'requestBody': {
                        'required': True,
                        'content': {'application/json': {'schema': {'$ref': '#/components/schemas/PickerSessionRequest'}}},
                    },
                    'responses': {
                        '201': {'description': '短效挑選工作階段與可嵌入 /picker 網址'},
                        '403': {'description': '來源網域未獲授權', 'content': {'application/json': {'schema': error_ref}}},
                    },
                },
            },
            '/mcp': {
                'post': {
                    'tags': ['MCP'],
                    'summary': 'HTTP MCP JSON-RPC 2.0 端點（list_photo_datasets / search_photos / get_photo_previews / get_photo_details）',
                    'security': [{'BearerAuth': []}],
                    'responses': {
                        '200': {'description': 'JSON-RPC 2.0 回應（get_photo_previews 回傳 MCP image Base64 區塊）'},
                        '401': {'description': '未授權，附 WWW-Authenticate Bearer resource_metadata 標頭'},
                    },
                },
            },
        },
        'x-integration-scopes': list(INTEGRATION_SCOPES),
    }
