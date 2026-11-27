"""Management command to run retention cleanup for Integration API / MCP / Picker artifacts."""
import json
from django.core.management.base import BaseCommand, CommandError
from core.integration_maintenance import (
    DEFAULT_AUDIT_DETAIL_DAYS,
    DEFAULT_REPORT_RETENTION_DAYS,
    DEFAULT_TOKEN_RETENTION_DAYS,
    run_integration_cleanup,
)


class Command(BaseCommand):
    help = '依保留政策清理過期的報告用圖副本、已過期／撤銷憑證與挑選工作階段，並去識別化逾齡稽核查詢字詞。'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='僅預覽可清理項目，不刪除或修改任何資料')
        parser.add_argument('--report-retention-days', type=int, default=DEFAULT_REPORT_RETENTION_DAYS, help='報告副本實體檔案保留天數（預設 30）')
        parser.add_argument('--token-retention-days', type=int, default=DEFAULT_TOKEN_RETENTION_DAYS, help='過期／撤銷憑證與挑選工作階段保留天數（預設 30）')
        parser.add_argument('--audit-detail-days', type=int, default=DEFAULT_AUDIT_DETAIL_DAYS, help='整合稽核查詢字詞保留天數（預設 180）')
        parser.add_argument('--json', action='store_true', dest='as_json', help='以 JSON 格式輸出結果')

    def handle(self, *args, **options):
        try:
            result = run_integration_cleanup(
                report_retention_days=options['report_retention_days'],
                token_retention_days=options['token_retention_days'],
                audit_detail_days=options['audit_detail_days'],
                dry_run=options['dry_run'],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options['as_json']:
            self.stdout.write(json.dumps(result, ensure_ascii=False))
            return

        prefix = '[DRY-RUN] ' if result['dry_run'] else ''
        self.stdout.write(
            f"{prefix}報告副本清理：{result['expired_report_files']} 檔（釋放 {result['freed_bytes']} bytes），"
            f"授權碼：{result['deleted_auth_codes']}，憑證：{result['deleted_tokens']}，"
            f"挑選工作階段：{result['deleted_picker_sessions']}，稽核字詞遮罩：{result['redacted_audits']}"
        )
