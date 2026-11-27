(function () {
  'use strict';

  const root = document.getElementById('picker-root');
  const qs = new URLSearchParams(window.location.search);
  const sessionToken = qs.get('session') || '';
  const requestId = qs.get('request_id') || '';
  // Remove short-lived session token from URL bar/history once captured in memory
  if (sessionToken && window.history && window.history.replaceState) {
    qs.delete('session');
    const clean = window.location.pathname + (qs.toString() ? '?' + qs.toString() : '');
    window.history.replaceState(null, '', clean);
  }

  const state = {
    boot: null,
    loading: false,
    error: '',
    banner: '',
    mode: 'general',
    dataset: '',
    from: '',
    to: '',
    county: '',
    text: '',
    query: '',
    nearbyLat: '',
    nearbyLon: '',
    nearbyRadius: '500',
    tileSize: 190,
    limit: 20,
    offset: 0,
    result: null,
    // Independent selection store (survives pagination, zoom, and filter changes)
    selectedOrder: [],
    selectedMap: {},
    previewPhoto: null,
    confirmModal: null,
    importing: false,
    importIdemKey: '',
  };

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function apiFetch(action, method, body) {
    const opts = {
      method: method || 'POST',
      headers: {
        'X-Picker-Session': sessionToken,
      },
    };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch('/integration/picker/' + action, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const msg = (data && data.error && data.error.message) || '請求失敗（HTTP ' + resp.status + '）';
      const code = (data && data.error && data.error.code) || 'ERROR';
      const err = new Error(msg);
      err.code = code;
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  function warnLabel(w) {
    const map = {
      missing_captured_time: '缺拍攝時間',
      unconfirmed_captured_time: '拍攝時間未確認',
      missing_location: '缺座標',
      dataset_archived: '資料集已封存',
    };
    return map[w] || w;
  }

  function toggleSelect(photo) {
    const id = photo.photo_id;
    if (state.selectedMap[id]) {
      delete state.selectedMap[id];
      state.selectedOrder = state.selectedOrder.filter((x) => x !== id);
    } else {
      const maxSel = (state.boot && state.boot.max_select) || 50;
      if (state.selectedOrder.length >= maxSel) {
        state.banner = '已達本次最多選取上限（' + maxSel + ' 張）';
        render();
        return;
      }
      state.selectedOrder.push(id);
      state.selectedMap[id] = {
        photo_id: id,
        version: photo.version,
        dataset_name: photo.dataset_name,
        name: photo.name,
        captured: photo.captured,
        ticket_url: (photo.thumbnail && photo.thumbnail.ticket && photo.thumbnail.ticket.url) || '',
        quality_warnings: photo.quality_warnings || [],
        caption: photo.name || '',
      };
    }
    state.importIdemKey = '';
    render();
  }

  function moveSelected(idx, delta) {
    const next = idx + delta;
    if (next < 0 || next >= state.selectedOrder.length) return;
    const arr = state.selectedOrder.slice();
    const tmp = arr[idx];
    arr[idx] = arr[next];
    arr[next] = tmp;
    state.selectedOrder = arr;
    state.importIdemKey = '';
    render();
  }

  async function runSearch(resetOffset) {
    if (resetOffset) state.offset = 0;
    state.loading = true;
    state.error = '';
    state.banner = '';
    render();
    try {
      const payload = {
        mode: state.mode,
        limit: state.limit,
        offset: state.offset,
      };
      if (state.dataset) payload.datasets = [Number(state.dataset)];
      if (state.from) payload.from = state.from;
      if (state.to) payload.to = state.to;
      if (state.county) payload.county = state.county;
      if (state.mode === 'general' && state.text) payload.text = state.text;
      if ((state.mode === 'ocr' || state.mode === 'semantic') && state.query) payload.query = state.query;
      if (state.mode === 'nearby' && state.nearbyLat && state.nearbyLon) {
        payload.nearby = {
          lat: Number(state.nearbyLat),
          lon: Number(state.nearbyLon),
          radius_m: Number(state.nearbyRadius || 500),
        };
      }
      state.result = await apiFetch('search', 'POST', payload);
    } catch (err) {
      state.error = err.message;
      state.result = null;
    } finally {
      state.loading = false;
      render();
    }
  }

  async function openConfirmModal() {
    if (!state.selectedOrder.length) return;
    state.loading = true;
    state.error = '';
    render();
    try {
      const expected = {};
      state.selectedOrder.forEach((id) => {
        expected[String(id)] = state.selectedMap[id].version;
      });
      const resolved = await apiFetch('resolve', 'POST', {
        photo_ids: state.selectedOrder,
        expected_versions: expected,
      });
      const byId = {};
      (resolved.results || []).forEach((r) => {
        byId[r.photo_id] = r;
      });
      state.confirmModal = {
        checkedAt: new Date().toISOString(),
        items: state.selectedOrder.map((id, i) => ({
          ordinal: i + 1,
          selected: state.selectedMap[id],
          resolved: byId[id] || { status: 'unavailable', message: '無法驗證最新狀態' },
        })),
      };
      if (!state.importIdemKey) {
        state.importIdemKey = 'pk-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
      }
    } catch (err) {
      state.error = err.message;
    } finally {
      state.loading = false;
      render();
    }
  }

  function hasBlockingIssues() {
    if (!state.confirmModal) return true;
    return state.confirmModal.items.some(
      (x) => x.resolved.status !== 'available' || x.resolved.version_changed
    );
  }

  function acceptUpdatedVersion(photoId, newPhoto) {
    if (state.selectedMap[photoId]) {
      state.selectedMap[photoId].version = newPhoto.version;
      state.selectedMap[photoId].captured = newPhoto.captured;
      state.selectedMap[photoId].quality_warnings = newPhoto.quality_warnings || [];
      if (newPhoto.thumbnail && newPhoto.thumbnail.ticket) {
        state.selectedMap[photoId].ticket_url = newPhoto.thumbnail.ticket.url;
      }
    }
    state.importIdemKey = '';
    openConfirmModal();
  }

  function removeInvalidItem(photoId) {
    delete state.selectedMap[photoId];
    state.selectedOrder = state.selectedOrder.filter((x) => x !== photoId);
    state.importIdemKey = '';
    if (!state.selectedOrder.length) {
      state.confirmModal = null;
      render();
    } else {
      openConfirmModal();
    }
  }

  async function submitImport() {
    if (state.importing || hasBlockingIssues()) return;
    state.importing = true;
    state.error = '';
    render();
    try {
      const items = state.selectedOrder.map((id, idx) => ({
        photo_id: id,
        expected_version: state.selectedMap[id].version,
        ordinal: idx + 1,
        caption: state.selectedMap[id].caption || '',
        variant: 'report',
      }));
      const result = await apiFetch('import', 'POST', {
        idempotency_key: state.importIdemKey,
        items,
      });
      state.confirmModal = null;
      state.banner =
        '匯入作業完成（狀態：' +
        result.state +
        '，成功 ' +
        result.summary.ready +
        ' / ' +
        result.summary.total +
        ' 張，作業編號：' +
        result.import_job_id +
        '）';
      // Notify embedding parent/opener ONLY with validated origin (never '*')
      const targetOrigin = state.boot && state.boot.origin;
      const msg = {
        type: 'photo-picker:imported',
        request_id: requestId,
        import_job_id: result.import_job_id,
        result,
      };
      if (targetOrigin) {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage(msg, targetOrigin);
        } else if (window.opener) {
          window.opener.postMessage(msg, targetOrigin);
        }
      }
    } catch (err) {
      state.error = err.message;
    } finally {
      state.importing = false;
      render();
    }
  }

  function render() {
    if (!sessionToken) {
      root.innerHTML = '<div style="padding:24px"><h3>缺少挑選工作階段</h3><p>請由業務系統開啟有效的照片挑選器工作階段。</p></div>';
      return;
    }
    if (!state.boot) {
      root.innerHTML = state.error
        ? '<div style="padding:24px"><h3 style="color:#b91c1c">無法啟動照片挑選器</h3><p>' + esc(state.error) + '</p></div>'
        : '<div style="padding:24px">載入挑選工作階段中…</div>';
      return;
    }

    const caps = state.boot.capabilities || {};
    const items = (state.result && state.result.items) || [];
    const total = (state.result && state.result.total) || 0;
    const maxSel = state.boot.max_select || 50;

    let html = '<div class="pk-wrap">';
    html += '<div class="pk-main">';
    html +=
      '<div class="pk-bar">' +
      '<div><strong>勘災報告照片挑選器</strong> ｜ 系統：' + esc(state.boot.system.name) +
      ' ｜ 人員：' + esc(state.boot.actor.username) +
      (state.boot.case_id ? ' ｜ 案件：' + esc(state.boot.case_id) : '') +
      (state.boot.report_id ? ' ｜ 報告：' + esc(state.boot.report_id) : '') +
      '</div>' +
      '<div>縮圖大小 <input id="pk-tile-range" type="range" min="130" max="280" value="' + state.tileSize + '"></div>' +
      '</div>';

    if (state.banner) {
      html += '<div class="pk-banner">' + esc(state.banner) + '</div>';
    }
    if (state.error) {
      html += '<div class="pk-banner pk-err">' + esc(state.error) + ' <button class="pk-btn" id="pk-retry">重試</button></div>';
    }

    html += '<div class="pk-filters">';
    html += '<select id="pk-mode">';
    if (caps.search) html += '<option value="general"' + (state.mode === 'general' ? ' selected' : '') + '>一般搜尋</option>';
    if (caps.nearby) html += '<option value="nearby"' + (state.mode === 'nearby' ? ' selected' : '') + '>附近搜尋</option>';
    if (caps.ocr_search) html += '<option value="ocr"' + (state.mode === 'ocr' ? ' selected' : '') + '>OCR 文字搜尋</option>';
    if (caps.semantic_search) html += '<option value="semantic"' + (state.mode === 'semantic' ? ' selected' : '') + '>影像內容搜尋</option>';
    html += '</select>';

    html += '<select id="pk-dataset"><option value="">全部授權資料集</option>';
    (state.boot.datasets || []).forEach((d) => {
      html += '<option value="' + d.id + '"' + (String(state.dataset) === String(d.id) ? ' selected' : '') + '>' + esc(d.name) + ' (' + d.submitted_photo_count + ')</option>';
    });
    html += '</select>';

    html += '<input id="pk-from" type="date" value="' + esc(state.from) + '" title="拍攝起日">';
    html += '<span>～</span>';
    html += '<input id="pk-to" type="date" value="' + esc(state.to) + '" title="拍攝迄日（含當天）">';
    html += '<input id="pk-county" type="text" placeholder="縣市" style="width:88px" value="' + esc(state.county) + '">';

    if (state.mode === 'general') {
      html += '<input id="pk-text" type="text" placeholder="檔名關鍵字" value="' + esc(state.text) + '">';
    } else if (state.mode === 'ocr' || state.mode === 'semantic') {
      html += '<input id="pk-query" type="text" placeholder="' + (state.mode === 'ocr' ? '輸入圖片文字' : '輸入中文影像內容') + '" value="' + esc(state.query) + '">';
    } else if (state.mode === 'nearby') {
      html += '<input id="pk-lat" type="text" placeholder="緯度" style="width:90px" value="' + esc(state.nearbyLat) + '">';
      html += '<input id="pk-lon" type="text" placeholder="經度" style="width:90px" value="' + esc(state.nearbyLon) + '">';
      html += '<input id="pk-rad" type="number" placeholder="公尺" style="width:76px" value="' + esc(state.nearbyRadius) + '">';
    }
    html += '<button class="pk-btn primary" id="pk-search-btn"' + (state.loading ? ' disabled' : '') + '>' + (state.loading ? '搜尋中…' : '搜尋') + '</button>';
    html += '</div>';

    html += '<div class="pk-wall" style="--pk-tile:' + state.tileSize + 'px">';
    if (!items.length && !state.loading) {
      html += '<div style="grid-column:1/-1;color:#64748b;padding:24px">目前條件無符合的授權照片。</div>';
    }
    items.forEach((p) => {
      const sel = Boolean(state.selectedMap[p.photo_id]);
      const thumbUrl = (p.thumbnail && p.thumbnail.ticket && p.thumbnail.ticket.url) || '';
      html += '<div class="pk-card' + (sel ? ' selected' : '') + '">';
      html += '<img class="pk-thumb" data-preview="' + p.photo_id + '" src="' + esc(thumbUrl) + '" alt="' + esc(p.name) + '">';
      html += '<div class="pk-meta">';
      html += '<div><strong>#' + p.photo_id + '</strong> ' + esc(p.name) + '</div>';
      html += '<div><span class="pk-badge">' + esc(p.dataset_name) + '</span> ' + esc(p.captured ? p.captured.slice(0, 16).replace('T', ' ') : '無拍攝時間') + '</div>';
      if (p.match_reason && p.match_reason.snippet) {
        html += '<div style="color:#0369a1">OCR：' + esc(p.match_reason.snippet) + '</div>';
      }
      if (p.match_reason && p.match_reason.score !== undefined) {
        html += '<div style="color:#0369a1">相近度：' + p.match_reason.score + '</div>';
      }
      if (p.match_reason && p.match_reason.distance_m !== undefined) {
        html += '<div style="color:#0369a1">距離：約 ' + esc(p.match_reason.distance_m) + ' 公尺</div>';
      }
      if (p.quality_warnings && p.quality_warnings.length) {
        html += '<div>' + p.quality_warnings.map((w) => '<span class="pk-badge pk-warn">' + esc(warnLabel(w)) + '</span>').join(' ') + '</div>';
      }
      html += '<div style="margin-top:4px;display:flex;gap:6px">';
      html += '<button class="pk-btn ' + (sel ? 'primary' : '') + '" data-toggle="' + p.photo_id + '">' + (sel ? '✓ 已選' : '＋ 選取') + '</button>';
      html += '<button class="pk-btn" data-preview="' + p.photo_id + '">大圖</button>';
      html += '</div></div></div>';
    });
    html += '</div>';

    html += '<div class="pk-pager">';
    html += '<div>共 ' + total + ' 筆授權結果（本頁 ' + items.length + ' 筆）' + (state.result && state.result.excluded_missing_time ? '，排除無拍攝時間 ' + state.result.excluded_missing_time + ' 筆' : '') + '</div>';
    html += '<div>';
    html += '<button class="pk-btn" id="pk-prev"' + (state.offset <= 0 ? ' disabled' : '') + '>上一頁</button> ';
    html += '<button class="pk-btn" id="pk-next"' + (!(state.result && state.result.has_next) ? ' disabled' : '') + '>下一頁</button>';
    html += '</div></div>';
    html += '</div>'; // end pk-main

    // Right tray: independent selected list
    html += '<div class="pk-tray">';
    html += '<div class="pk-tray-head"><span>已選清單 (' + state.selectedOrder.length + ' / ' + maxSel + ')</span>';
    if (state.selectedOrder.length) {
      html += '<button class="pk-btn" id="pk-clear">清空</button>';
    }
    html += '</div>';
    html += '<div class="pk-tray-list">';
    if (!state.selectedOrder.length) {
      html += '<div style="color:#64748b;font-size:13px">尚未選取照片。換頁、調整縮圖或更改篩選條件都不會清除已選清單。</div>';
    }
    state.selectedOrder.forEach((id, idx) => {
      const it = state.selectedMap[id];
      html += '<div class="pk-tray-item">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
      html += '<strong>' + (idx + 1) + '. #' + id + ' ' + esc(it.name) + '</strong>';
      html += '<span>';
      html += '<button class="pk-btn" data-up="' + idx + '"' + (idx === 0 ? ' disabled' : '') + '>↑</button>';
      html += '<button class="pk-btn" data-down="' + idx + '"' + (idx === state.selectedOrder.length - 1 ? ' disabled' : '') + '>↓</button>';
      html += '<button class="pk-btn" data-remove="' + id + '">✕</button>';
      html += '</span></div>';
      html += '<div style="color:#475569;margin-bottom:4px">' + esc(it.dataset_name) + ' ｜ v' + it.version + '</div>';
      html += '<input type="text" data-caption="' + id + '" value="' + esc(it.caption) + '" placeholder="照片說明（可修改）" style="width:100%;box-sizing:border-box;padding:4px 6px;border:1px solid #cbd5e1;border-radius:4px">';
      html += '</div>';
    });
    html += '</div>';
    html += '<div class="pk-tray-foot">';
    html += '<button class="pk-btn primary" style="flex:1" id="pk-open-confirm"' + (!state.selectedOrder.length ? ' disabled' : '') + '>檢視並匯入報告 (' + state.selectedOrder.length + ')</button>';
    html += '</div></div></div>';

    // Large preview modal
    if (state.previewPhoto) {
      const p = state.previewPhoto;
      const turl = (p.thumbnail && p.thumbnail.ticket && p.thumbnail.ticket.url) || '';
      html += '<div class="pk-modal-bg" id="pk-preview-bg"><div class="pk-modal">';
      html += '<div class="pk-modal-body">';
      html += '<h3>#' + p.photo_id + ' ' + esc(p.name) + ' (v' + p.version + ')</h3>';
      html += '<img src="' + esc(turl) + '" style="max-width:100%;max-height:55vh;display:block;margin:0 auto 12px">';
      html += '<p>資料集：' + esc(p.dataset_name) + ' ｜ 拍攝時間：' + esc(p.captured || '無') + ' (' + esc(p.timezone) + ')</p>';
      if (p.location) {
        html += '<p>位置：' + esc([p.location.county, p.location.town, p.location.village].filter(Boolean).join(' ')) + (p.location.lat != null ? ' (' + p.location.lat + ', ' + p.location.lon + ')' : '（無座標）') + '</p>';
      }
      html += '</div><div class="pk-modal-foot"><button class="pk-btn" id="pk-close-preview">關閉</button></div></div></div>';
    }

    // Confirm import modal (re-verified against latest auth & version)
    if (state.confirmModal) {
      const blocking = hasBlockingIssues();
      html += '<div class="pk-modal-bg"><div class="pk-modal">';
      html += '<div class="pk-modal-body">';
      html += '<h3>確認匯入勘災報告（共 ' + state.confirmModal.items.length + ' 張）</h3>';
      if (blocking) {
        html += '<div class="pk-banner pk-err" style="margin-bottom:10px">偵測到失效、失權或版本已變更的照片。系統不會靜默略過或自動替換，請逐一重新檢閱或移除後再匯入。</div>';
      }
      state.confirmModal.items.forEach((row) => {
        const res = row.resolved;
        const ok = res.status === 'available' && !res.version_changed;
        html += '<div style="border:1px solid ' + (ok ? '#cbd5e1' : '#dc2626') + ';border-radius:6px;padding:8px;margin-bottom:8px;display:flex;gap:10px;align-items:center">';
        if (row.selected.ticket_url) {
          html += '<img src="' + esc(row.selected.ticket_url) + '" style="width:72px;height:54px;object-fit:cover;border-radius:4px">';
        }
        html += '<div style="flex:1;font-size:12px">';
        html += '<div><strong>' + row.ordinal + '. #' + row.selected.photo_id + ' ' + esc(row.selected.name) + '</strong>（' + esc(row.selected.dataset_name) + '）</div>';
        html += '<div>拍攝時間：' + esc(row.selected.captured || '無') + ' ｜ 說明：' + esc(row.selected.caption || '（無）') + '</div>';
        if (row.selected.quality_warnings && row.selected.quality_warnings.length) {
          html += '<div>' + row.selected.quality_warnings.map((w) => '<span class="pk-badge pk-warn">' + esc(warnLabel(w)) + '</span>').join(' ') + '</div>';
        }
        if (res.status !== 'available') {
          html += '<div style="color:#b91c1c;margin-top:4px">狀態失效：' + esc(res.message || '照片已回收或失權') + ' <button class="pk-btn danger" data-drop="' + row.selected.photo_id + '">移除</button></div>';
        } else if (res.version_changed) {
          html += '<div style="color:#b91c1c;margin-top:4px">版本已變更（原 v' + row.selected.version + ' → 最新 v' + res.photo.version + '） ' +
            '<button class="pk-btn primary" data-accept-ver="' + row.selected.photo_id + '">檢閱並採用 v' + res.photo.version + '</button> ' +
            '<button class="pk-btn danger" data-drop="' + row.selected.photo_id + '">移除</button></div>';
        }
        html += '</div></div>';
      });
      html += '</div><div class="pk-modal-foot">';
      html += '<button class="pk-btn" id="pk-cancel-confirm">返回修改</button>';
      html += '<button class="pk-btn primary" id="pk-submit-import"' + (blocking || state.importing ? ' disabled' : '') + '>' + (state.importing ? '匯入中…' : '確認匯入報告') + '</button>';
      html += '</div></div></div>';
    }

    root.innerHTML = html;
    bindEvents();
  }

  function bindEvents() {
    const byId = (id) => document.getElementById(id);
    if (byId('pk-tile-range')) {
      byId('pk-tile-range').addEventListener('input', (e) => {
        state.tileSize = Number(e.target.value);
        const wall = root.querySelector('.pk-wall');
        if (wall) wall.style.setProperty('--pk-tile', state.tileSize + 'px');
      });
    }
    if (byId('pk-mode')) byId('pk-mode').addEventListener('change', (e) => { state.mode = e.target.value; render(); });
    if (byId('pk-dataset')) byId('pk-dataset').addEventListener('change', (e) => { state.dataset = e.target.value; });
    if (byId('pk-from')) byId('pk-from').addEventListener('change', (e) => { state.from = e.target.value; });
    if (byId('pk-to')) byId('pk-to').addEventListener('change', (e) => { state.to = e.target.value; });
    if (byId('pk-county')) byId('pk-county').addEventListener('input', (e) => { state.county = e.target.value; });
    if (byId('pk-text')) byId('pk-text').addEventListener('input', (e) => { state.text = e.target.value; });
    if (byId('pk-query')) byId('pk-query').addEventListener('input', (e) => { state.query = e.target.value; });
    if (byId('pk-lat')) byId('pk-lat').addEventListener('input', (e) => { state.nearbyLat = e.target.value; });
    if (byId('pk-lon')) byId('pk-lon').addEventListener('input', (e) => { state.nearbyLon = e.target.value; });
    if (byId('pk-rad')) byId('pk-rad').addEventListener('input', (e) => { state.nearbyRadius = e.target.value; });
    if (byId('pk-search-btn')) byId('pk-search-btn').addEventListener('click', () => runSearch(true));
    if (byId('pk-retry')) byId('pk-retry').addEventListener('click', () => runSearch(false));
    if (byId('pk-prev')) byId('pk-prev').addEventListener('click', () => { state.offset = Math.max(0, state.offset - state.limit); runSearch(false); });
    if (byId('pk-next')) byId('pk-next').addEventListener('click', () => { if (state.result && state.result.next_offset != null) { state.offset = state.result.next_offset; runSearch(false); } });
    if (byId('pk-clear')) byId('pk-clear').addEventListener('click', () => { state.selectedOrder = []; state.selectedMap = {}; state.importIdemKey = ''; render(); });
    if (byId('pk-open-confirm')) byId('pk-open-confirm').addEventListener('click', openConfirmModal);
    if (byId('pk-close-preview')) byId('pk-close-preview').addEventListener('click', () => { state.previewPhoto = null; render(); });
    if (byId('pk-cancel-confirm')) byId('pk-cancel-confirm').addEventListener('click', () => { state.confirmModal = null; render(); });
    if (byId('pk-submit-import')) byId('pk-submit-import').addEventListener('click', submitImport);

    const itemsById = {};
    ((state.result && state.result.items) || []).forEach((p) => { itemsById[p.photo_id] = p; });

    root.querySelectorAll('[data-toggle]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pid = Number(btn.getAttribute('data-toggle'));
        if (itemsById[pid]) toggleSelect(itemsById[pid]);
      });
    });
    root.querySelectorAll('[data-preview]').forEach((el) => {
      el.addEventListener('click', () => {
        const pid = Number(el.getAttribute('data-preview'));
        if (itemsById[pid]) { state.previewPhoto = itemsById[pid]; render(); }
      });
    });
    root.querySelectorAll('[data-up]').forEach((btn) => {
      btn.addEventListener('click', () => moveSelected(Number(btn.getAttribute('data-up')), -1));
    });
    root.querySelectorAll('[data-down]').forEach((btn) => {
      btn.addEventListener('click', () => moveSelected(Number(btn.getAttribute('data-down')), 1));
    });
    root.querySelectorAll('[data-remove]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pid = Number(btn.getAttribute('data-remove'));
        delete state.selectedMap[pid];
        state.selectedOrder = state.selectedOrder.filter((x) => x !== pid);
        state.importIdemKey = '';
        render();
      });
    });
    root.querySelectorAll('[data-caption]').forEach((inp) => {
      inp.addEventListener('input', () => {
        const pid = Number(inp.getAttribute('data-caption'));
        if (state.selectedMap[pid]) {
          state.selectedMap[pid].caption = inp.value;
          state.importIdemKey = '';
        }
      });
    });
    root.querySelectorAll('[data-drop]').forEach((btn) => {
      btn.addEventListener('click', () => removeInvalidItem(Number(btn.getAttribute('data-drop'))));
    });
    root.querySelectorAll('[data-accept-ver]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pid = Number(btn.getAttribute('data-accept-ver'));
        const found = state.confirmModal && state.confirmModal.items.find((x) => x.selected.photo_id === pid);
        if (found && found.resolved && found.resolved.photo) {
          acceptUpdatedVersion(pid, found.resolved.photo);
        }
      });
    });
  }

  async function bootstrap() {
    if (!sessionToken) {
      render();
      return;
    }
    try {
      state.boot = await apiFetch('bootstrap', 'GET');
      const caps = state.boot.capabilities || {};
      const d = state.boot.defaults || {};
      if (d.mode) {
        state.mode = d.mode;
      } else if (!caps.search) {
        if (caps.nearby) state.mode = 'nearby';
        else if (caps.ocr_search) state.mode = 'ocr';
        else if (caps.semantic_search) state.mode = 'semantic';
      }
      if (d.dataset_id) state.dataset = String(d.dataset_id);
      if (d.from) state.from = d.from;
      if (d.to) state.to = d.to;
      if (d.county) state.county = d.county;
      if (d.text) state.text = d.text;
      if (d.query) state.query = d.query;
      if (d.nearby && d.nearby.lat != null && d.nearby.lon != null) {
        state.nearbyLat = String(d.nearby.lat);
        state.nearbyLon = String(d.nearby.lon);
        if (d.nearby.radius_m) state.nearbyRadius = String(d.nearby.radius_m);
      }
      render();
      runSearch(true);
    } catch (err) {
      state.error = err.message;
      render();
    }
  }

  bootstrap();
})();
