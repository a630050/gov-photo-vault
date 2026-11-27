(function () {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const state = {
    datasets: [],
    candidates: [],
    importedKeys: new Set(),
    importedPhotos: [],
    failedImports: {},
    activeRequestId: '',
  };

  function log(msg) {
    const el = byId('dm-log');
    if (el) el.textContent = '[' + new Date().toLocaleTimeString() + '] ' + msg + '\n' + el.textContent;
  }

  function token() {
    return (byId('dm-token').value || '').trim();
  }

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function mcpCall(method, params) {
    const resp = await fetch('/mcp', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + token(),
      },
      body: JSON.stringify({ jsonrpc: '2.0', id: Date.now(), method, params }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      throw new Error((data.error && data.error.message) || 'MCP 呼叫失敗');
    }
    return data.result;
  }

  async function loadCaps() {
    if (!token()) return alert('請先輸入存取憑證');
    try {
      const res = await mcpCall('tools/call', { name: 'list_photo_datasets', arguments: {} });
      const sc = res.structuredContent || {};
      state.datasets = sc.datasets || [];
      byId('dm-caps-info').textContent =
        '已透過 MCP 連線，授權資料集共 ' + state.datasets.length + ' 個。';
      const sel = byId('dm-ai-ds');
      sel.innerHTML = '<option value="">全部授權資料集</option>' +
        state.datasets.map((d) => '<option value="' + d.id + '">' + esc(d.name) + ' (' + d.submitted_photo_count + ')</option>').join('');
      log('MCP list_photo_datasets 成功，取得 ' + state.datasets.length + ' 個資料集');
    } catch (err) {
      byId('dm-caps-info').textContent = '連線失敗：' + err.message;
      log('MCP 錯誤：' + err.message);
    }
  }

  async function runAiSearch() {
    if (!token()) return alert('請先輸入存取憑證');
    const mode = byId('dm-ai-mode').value;
    const ds = byId('dm-ai-ds').value;
    const from = byId('dm-ai-from').value;
    const to = byId('dm-ai-to').value;
    const q = byId('dm-ai-query').value.trim();

    if ((mode === 'ocr' || mode === 'semantic') && !q) {
      byId('dm-ai-msg').textContent = '請補充搜尋關鍵字，AI 不得自行捏造條件。';
      return;
    }

    const args = { mode, limit: 8 };
    if (ds) args.datasets = [Number(ds)];
    if (from) args.from = from;
    if (to) args.to = to;
    if (mode === 'general' && q) args.text = q;
    if ((mode === 'ocr' || mode === 'semantic') && q) args.query = q;
    if (mode === 'nearby') {
      const lat = (byId('dm-ai-lat').value || '').trim();
      const lon = (byId('dm-ai-lon').value || '').trim();
      const rad = Number(byId('dm-ai-rad').value || 500);
      if (!lat || !lon) {
        byId('dm-ai-msg').textContent = '請補充可靠的設施或案件座標（緯度、經度），AI 不得自行捏造位置。';
        return;
      }
      args.nearby = { lat: Number(lat), lon: Number(lon), radius_m: rad };
    }

    byId('dm-ai-msg').textContent = '呼叫 MCP search_photos 與 get_photo_previews 中…';
    try {
      const sRes = await mcpCall('tools/call', { name: 'search_photos', arguments: args });
      if (sRes.isError) throw new Error((sRes.structuredContent && sRes.structuredContent.error && sRes.structuredContent.error.message) || '搜尋失敗');
      const sData = sRes.structuredContent;
      const items = sData.items || [];
      const searchItemById = {};
      items.forEach((it) => { searchItemById[it.photo_id] = it; });
      byId('dm-ai-msg').textContent =
        '搜尋編號 ' + sData.search_id + '：共 ' + sData.total + ' 筆授權結果（顯示 ' + items.length + ' 筆）';
      if (!items.length) {
        byId('dm-ai-cards').innerHTML = '<div style="color:#64748b">無符合結果。</div>';
        return;
      }
      const pRes = await mcpCall('tools/call', {
        name: 'get_photo_previews',
        arguments: { photo_ids: items.map((x) => x.photo_id) },
      });
      const previews = (pRes.structuredContent && pRes.structuredContent.previews) || [];
      const imgBlocks = (pRes.content || []).filter((c) => c.type === 'image');
      let imgIdx = 0;
      const warnLabels = {
        missing_captured_time: '缺拍攝時間',
        unconfirmed_captured_time: '時間未確認',
        missing_location: '缺座標',
        dataset_archived: '資料集已封存',
      };
      const html = previews.map((entry) => {
        if (entry.status !== 'ready') return '';
        const img = imgBlocks[imgIdx++];
        const src = img ? 'data:' + img.mimeType + ';base64,' + img.data : '';
        const p = entry.photo;
        const sItem = searchItemById[p.photo_id] || {};
        const mr = sItem.match_reason || p.match_reason || {};
        const ticketUrl = p.thumbnail && p.thumbnail.ticket ? p.thumbnail.ticket.url : '';
        let reasonHtml = '';
        if (mr.snippet) reasonHtml = '<div style="color:#0369a1">OCR 命中：' + esc(mr.snippet) + '</div>';
        else if (mr.score !== undefined) reasonHtml = '<div style="color:#0369a1">內容相近度：' + esc(mr.score) + '</div>';
        else if (mr.distance_m !== undefined) reasonHtml = '<div style="color:#0369a1">距離：約 ' + esc(mr.distance_m) + ' 公尺</div>';
        const warnings = (p.quality_warnings || []).map((w) => warnLabels[w] || w);
        const warnHtml = warnings.length ? '<div style="color:#b45309">品質提示：' + esc(warnings.join('、')) + '</div>' : '';
        return '<div class="dm-card">' +
          '<img src="' + src + '" alt="' + esc(p.name) + '">' +
          '<div class="dm-card-body">' +
          '<div><strong>#' + p.photo_id + '</strong> ' + esc(p.name) + '</div>' +
          '<div>' + esc(p.dataset_name) + ' ｜ ' + esc(p.captured ? p.captured.slice(0, 16).replace('T', ' ') : '無時間') + '</div>' +
          reasonHtml +
          warnHtml +
          '<div style="display:flex;gap:6px;margin-top:4px">' +
          (ticketUrl ? '<a class="dm-btn" href="' + esc(ticketUrl) + '" target="_blank" rel="noopener">開啟大圖</a>' : '') +
          '<button class="dm-btn primary" data-add-cand="' + p.photo_id + '">加入候選</button>' +
          '</div></div></div>';
      }).join('');
      byId('dm-ai-cards').innerHTML = html;
      byId('dm-ai-cards').querySelectorAll('[data-add-cand]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const id = Number(btn.getAttribute('data-add-cand'));
          if (!state.candidates.includes(id)) state.candidates.push(id);
          byId('dm-candidates').textContent = state.candidates.map((x) => '#' + x).join(', ');
        });
      });
      log('MCP AI 搜尋回圖完成，共 ' + imgBlocks.length + ' 張縮圖');
    } catch (err) {
      byId('dm-ai-msg').textContent = '錯誤：' + err.message;
      log('MCP 搜尋錯誤：' + err.message);
    }
  }

  async function openPicker() {
    if (!token()) return alert('請先輸入存取憑證');
    try {
      const resp = await fetch('/integration/v1/picker-sessions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + token(),
        },
        body: JSON.stringify({
          case_id: byId('dm-case-id').value.trim(),
          report_id: byId('dm-report-id').value.trim(),
          origin: window.location.origin,
          max_select: 50,
          defaults: {
            dataset_id: byId('dm-ai-ds').value ? Number(byId('dm-ai-ds').value) : undefined,
            from: byId('dm-ai-from').value || undefined,
            to: byId('dm-ai-to').value || undefined,
          },
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error((data.error && data.error.message) || '建立挑選工作階段失敗');
      state.activeRequestId = 'req-' + Date.now();
      const url = data.picker_url + '&request_id=' + encodeURIComponent(state.activeRequestId);
      byId('dm-modal-slot').innerHTML =
        '<div class="dm-iframe-bg"><div class="dm-iframe-box">' +
        '<div style="padding:8px 12px;background:#0f172a;color:#fff;display:flex;justify-content:space-between;align-items:center">' +
        '<span>照片挑選器（工作階段 ' + esc(data.session_id) + '）</span>' +
        '<button class="dm-btn" id="dm-close-picker">關閉</button></div>' +
        '<iframe src="' + esc(url) + '"></iframe></div></div>';
      byId('dm-close-picker').addEventListener('click', () => {
        state.activeRequestId = '';
        byId('dm-modal-slot').innerHTML = '';
      });
      log('已建立挑選工作階段 ' + data.session_id);
    } catch (err) {
      alert(err.message);
      log('開啟挑選器失敗：' + err.message);
    }
  }

  async function sha256Hex(buffer) {
    const hash = await crypto.subtle.digest('SHA-256', buffer);
    return Array.from(new Uint8Array(hash)).map((b) => b.toString(16).padStart(2, '0')).join('');
  }

  async function processImportResult(result) {
    const summary = result.summary || { ready: 0, total: 0, failed: 0 };
    byId('dm-report-status').textContent =
      '匯入作業 ' + result.import_job_id + '（狀態：' + result.state + '，成功 ' + summary.ready + ' / ' + summary.total + '）';
    log('收到挑選器匯入結果：' + result.import_job_id + ' (' + result.state + ')');

    for (const item of result.items || []) {
      const dedupKey = result.import_job_id + ':' + item.item_id;
      if (state.importedKeys.has(dedupKey)) {
        delete state.failedImports[dedupKey];
        continue;
      }
      if (item.state !== 'ready' || !item.output || !item.output.download_ticket) {
        const reason = item.error_message || '項目匯入失敗';
        state.failedImports[dedupKey] = { dedupKey, jobId: result.import_job_id, item, reason };
        log('項目 #' + item.photo_id + ' 匯入失敗：' + reason);
        continue;
      }
      const dlResp = await fetch(item.output.download_ticket.url, {
        headers: { Authorization: 'Bearer ' + token() },
      });
      if (!dlResp.ok) {
        const reason = '取圖失敗（HTTP ' + dlResp.status + '）';
        state.failedImports[dedupKey] = { dedupKey, jobId: result.import_job_id, item, reason };
        log('項目 #' + item.photo_id + ' ' + reason);
        continue;
      }
      const buf = await dlResp.arrayBuffer();
      const actualSha = await sha256Hex(buf);
      if (actualSha !== item.output.sha256 || buf.byteLength !== item.output.size) {
        const reason = '檔案雜湊或大小驗證失敗';
        state.failedImports[dedupKey] = { dedupKey, jobId: result.import_job_id, item, reason };
        log('項目 #' + item.photo_id + ' ' + reason + '！');
        continue;
      }
      delete state.failedImports[dedupKey];
      state.importedKeys.add(dedupKey);
      const mime = dlResp.headers.get('Content-Type') || 'image/jpeg';
      const blobUrl = URL.createObjectURL(new Blob([buf], { type: mime }));
      state.importedPhotos.push({ item, blobUrl, jobId: result.import_job_id });
    }
    renderImported();
  }

  async function retryImportJob(jobId) {
    if (!token()) return alert('請先輸入存取憑證');
    try {
      const resp = await fetch('/integration/v1/report-imports/' + encodeURIComponent(jobId) + '/retry', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + token(),
        },
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error((data.error && data.error.message) || '重試匯入失敗');
      log('已重試匯入作業 ' + jobId + '（狀態：' + data.state + '）');
      await processImportResult(data);
    } catch (err) {
      log('重試匯入失敗：' + err.message);
    }
  }

  window.addEventListener('message', async (event) => {
    if (event.origin !== window.location.origin) return;
    const msg = event.data;
    if (!state.activeRequestId || !msg || msg.type !== 'photo-picker:imported' || msg.request_id !== state.activeRequestId) return;
    state.activeRequestId = '';
    byId('dm-modal-slot').innerHTML = '';
    await processImportResult(msg.result);
  });

  function renderImported() {
    const okHtml = state.importedPhotos.map((entry) => {
      const it = entry.item;
      return '<div class="dm-card">' +
        '<img src="' + entry.blobUrl + '" alt="' + esc(it.caption) + '">' +
        '<div class="dm-card-body">' +
        '<div><strong>順序 ' + it.ordinal + ' ｜ #' + it.photo_id + ' (v' + it.actual_version + ')</strong></div>' +
        '<div>說明：' + esc(it.caption || '（無）') + '</div>' +
        '<div>資料集：' + esc(it.dataset_name) + '</div>' +
        '<div style="color:#15803d">✓ SHA-256 驗證通過 (' + it.output.size + ' bytes)</div>' +
        '</div></div>';
    }).join('');
    const failEntries = Object.values(state.failedImports);
    const failHtml = failEntries.map((entry) => {
      const it = entry.item;
      return '<div class="dm-card" style="border-color:#dc2626;background:#fef2f2">' +
        '<div class="dm-card-body">' +
        '<div><strong>順序 ' + it.ordinal + ' ｜ #' + it.photo_id + '（失敗）</strong></div>' +
        '<div style="color:#b91c1c">失敗原因：' + esc(entry.reason) + '</div>' +
        '<button class="dm-btn" data-retry-job="' + esc(entry.jobId) + '">重試失敗項目</button>' +
        '</div></div>';
    }).join('');
    byId('dm-report-items').innerHTML = okHtml + failHtml;
    byId('dm-report-items').querySelectorAll('[data-retry-job]').forEach((btn) => {
      btn.addEventListener('click', () => retryImportJob(btn.getAttribute('data-retry-job')));
    });
  }

  byId('dm-ai-mode').addEventListener('change', () => {
    const row = byId('dm-nearby-row');
    if (row) row.style.display = byId('dm-ai-mode').value === 'nearby' ? 'flex' : 'none';
  });
  byId('dm-load-caps').addEventListener('click', loadCaps);
  byId('dm-ai-run').addEventListener('click', runAiSearch);
  byId('dm-open-picker').addEventListener('click', openPicker);
})();
