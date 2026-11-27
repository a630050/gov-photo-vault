"""Run the photo archive MCP server over stdio using an issued OAuth Bearer token."""
import json
import os
import sys
from django.core.management.base import BaseCommand, CommandError
from core.integration_auth import IntegrationError, authenticate_raw_token
from core.mcp_server import handle_mcp_request


class Command(BaseCommand):
    help = '啟動照片歸檔系統 MCP stdio 伺服器（需提供受控存取憑證）'

    def add_arguments(self, parser):
        parser.add_argument('--token', default='', help='受控存取憑證（建議改由環境變數 PHOTO_MCP_TOKEN 提供）')

    def handle(self, *args, **options):
        raw_token = options['token'] or os.environ.get('PHOTO_MCP_TOKEN', '')
        if not raw_token:
            raise CommandError('請透過 --token 或環境變數 PHOTO_MCP_TOKEN 提供有效的 MCP 存取憑證')

        for line in sys.stdin:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                out = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
                sys.stdout.write(json.dumps(out, ensure_ascii=False) + '\n')
                sys.stdout.flush()
                continue

            try:
                ctx = authenticate_raw_token(raw_token, required_audience=('mcp', 'integration-api'), channel='mcp')
            except IntegrationError as exc:
                out = {
                    'jsonrpc': '2.0',
                    'id': payload.get('id') if isinstance(payload, dict) else None,
                    'error': {'code': -32001, 'message': exc.message, 'data': exc.to_dict()},
                }
                sys.stdout.write(json.dumps(out, ensure_ascii=False) + '\n')
                sys.stdout.flush()
                continue

            response = handle_mcp_request(ctx, payload)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
                sys.stdout.flush()
