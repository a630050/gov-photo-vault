"""Headless Chrome CDP benchmark & UI flow verifier against http://127.0.0.1:8000."""
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

CHROME_PATH = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
BASE_URL = 'http://127.0.0.1:8000'


class MiniCDP:
    def __init__(self, ws_url):
        u = urllib.parse.urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        req = (
            f'GET {u.path} HTTP/1.1\r\n'
            f'Host: {u.hostname}:{u.port}\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            'Sec-WebSocket-Version: 13\r\n\r\n'
        )
        self.sock.sendall(req.encode('ascii'))
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += self.sock.recv(4096)
        self.extra = buf.split(b'\r\n\r\n', 1)[1]
        self.seq = 0

    def _recv_exact(self, n):
        out = bytearray()
        if self.extra:
            take = self.extra[:n]
            out.extend(take)
            self.extra = self.extra[len(take):]
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise EOFError('CDP closed')
            out.extend(chunk)
        return bytes(out)

    def _send_text(self, text):
        data = text.encode('utf-8')
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack('!H', length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack('!Q', length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _recv_msg(self):
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin = (b1 >> 7) & 1
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack('!H', self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._recv_exact(8))[0]
            if b2 & 0x80:
                mask = self._recv_exact(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exact(length)))
            else:
                payload = self._recv_exact(length)
            if opcode == 0x8:
                raise EOFError('WS close')
            if opcode == 0x9:
                continue
            parts.append(payload)
            if fin:
                return b''.join(parts).decode('utf-8')

    def call(self, method, params=None):
        self.seq += 1
        cid = self.seq
        self._send_text(json.dumps({'id': cid, 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(self._recv_msg())
            if msg.get('id') == cid:
                if 'error' in msg:
                    raise RuntimeError(msg['error'])
                return msg.get('result', {})

    def eval(self, expr):
        res = self.call(
            'Runtime.evaluate',
            {'expression': expr, 'awaitPromise': True, 'returnByValue': True},
        )
        if 'exceptionDetails' in res:
            raise RuntimeError(res['exceptionDetails'])
        return res.get('result', {}).get('value')

    def close(self):
        self.sock.close()


def main():
    profile_dir = tempfile.mkdtemp(prefix='chrome_cdp_')
    port = 9229
    proc = subprocess.Popen(
        [
            CHROME_PATH,
            '--headless=new',
            f'--remote-debugging-port={port}',
            f'--user-data-dir={profile_dir}',
            '--window-size=1280,800',
            '--enable-precise-memory-info',
            '--no-first-run',
            '--no-default-browser-check',
            'about:blank',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp = None
    try:
        ws_url = None
        for _ in range(40):
            try:
                targets = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{port}/json').read().decode('utf-8'))
                page = next((t for t in targets if t.get('type') == 'page'), None)
                if page:
                    ws_url = page['webSocketDebuggerUrl']
                    break
            except Exception:
                time.sleep(0.25)
        assert ws_url, 'Chrome CDP did not start'
        cdp = MiniCDP(ws_url)
        cdp.call('Page.enable')
        cdp.call('Runtime.enable')
        cdp.call('Page.navigate', {'url': BASE_URL})
        for _ in range(30):
            if cdp.eval("!!document.querySelector('input[name=\"username\"]')"):
                break
            time.sleep(0.2)

        # Log in by clicking the submit button directly
        cdp.eval("""
            document.querySelector('input[name="username"]').value = 'member';
            document.querySelector('input[name="password"]').value = 'LocalPhoto!2026';
            document.querySelector('#login button[type="submit"], #login button.primary').click();
        """)
        for _ in range(30):
            if cdp.eval("!!document.querySelector('#cards')"):
                break
            time.sleep(0.2)
        time.sleep(0.5)

        # Switch tabs: library -> jobs -> inbox -> upload -> jobs -> library
        tab_trace = {}
        for t in ['jobs', 'inbox', 'upload', 'jobs', 'library']:
            cdp.eval(f"document.querySelector('nav button[data-tab=\"{t}\"]').click();")
            time.sleep(0.35)
            tab_trace[t] = cdp.eval("""({
                activeTab: document.querySelector('nav button.active')?.dataset?.tab || null,
                h1: document.querySelector('main h1')?.textContent || null
            })""")

        # Switch to photo wall and inject 10,000 items to measure virtual scroll DOM + JS heap
        cdp.eval("document.querySelector('[data-library-mode=\"wall\"]').click();")
        time.sleep(0.5)
        before_mem = cdp.eval("""({
            cardsCount: document.querySelectorAll('#cards .photo').length,
            totalDom: document.querySelectorAll('*').length,
            usedJSHeapMB: performance.memory ? +(performance.memory.usedJSHeapSize / 1048576).toFixed(2) : null
        })""")

        cdp.eval("""
            (() => {
                const sample = libraryState.items[0];
                libraryState.items = Array.from({length: 10000}, (_, i) => ({
                    ...sample,
                    id: 900000 + i,
                    name: 'bench_' + String(i).padStart(5, '0') + '.jpg',
                    burst_group: '',
                    tags: ['虛擬捲動']
                }));
                libraryState.total = 10000;
                drawLibraryResults(false);
            })()
        """)
        time.sleep(0.2)
        at_top = cdp.eval("""({
            cardsCount: document.querySelectorAll('#cards .photo').length,
            totalDom: document.querySelectorAll('*').length,
            usedJSHeapMB: performance.memory ? +(performance.memory.usedJSHeapSize / 1048576).toFixed(2) : null,
            firstVisibleName: document.querySelector('#cards .photo strong')?.textContent || null
        })""")

        cdp.eval("""
            (() => {
                const el = document.querySelector('#cards');
                el.scrollTop = Math.floor(el.scrollHeight * 0.5);
                el.dispatchEvent(new Event('scroll'));
            })()
        """)
        time.sleep(0.25)
        at_mid = cdp.eval("""({
            cardsCount: document.querySelectorAll('#cards .photo').length,
            totalDom: document.querySelectorAll('*').length,
            usedJSHeapMB: performance.memory ? +(performance.memory.usedJSHeapSize / 1048576).toFixed(2) : null,
            firstVisibleName: document.querySelector('#cards .photo strong')?.textContent || null,
            scrollTop: document.querySelector('#cards').scrollTop
        })""")

        # Resize thumbnail and check anchor index preservation
        cdp.eval("setLibraryPhotoSize(320, true);")
        time.sleep(0.15)
        after_resize = cdp.eval("""({
            cardsCount: document.querySelectorAll('#cards .photo').length,
            scrollTop: document.querySelector('#cards').scrollTop,
            firstVisibleName: document.querySelector('#cards .photo strong')?.textContent || null,
            sizeText: document.querySelector('#wall-size-value')?.textContent || null
        })""")

        print(json.dumps({
            'tab_trace': tab_trace,
            'before_10k': before_mem,
            'virtual_10k_top': at_top,
            'virtual_10k_mid': at_mid,
            'virtual_10k_after_resize': after_resize,
        }, ensure_ascii=False, indent=2))
    finally:
        if cdp:
            cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
