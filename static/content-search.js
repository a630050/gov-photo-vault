const contentUI={mode:'semantic',query:'',datasets:[],minimum:.20,rows:[],selected:new Set(),result:null,epoch:0,sequence:0,lastBatch:null,tagBusy:false};
function resetContentSearch(){Object.assign(contentUI,{mode:'semantic',query:'',datasets:[],minimum:.20,rows:[],selected:new Set(),result:null,lastBatch:null,tagBusy:false});contentUI.epoch++;contentUI.sequence++}
function renderContentSearch(){
 const s=contentUI,epoch=++s.epoch;s.sequence++;s.rows=[];s.selected.clear();s.result=null;
 s.datasets=s.datasets.filter(id=>state.datasets.some(d=>d.id===id));
 $('#side').innerHTML=`<h3>內容搜尋範圍</h3><p class="muted">僅搜尋有權閱覽的正式照片。未選資料集時，搜尋全部可閱覽資料集。</p><button id="content-all-scope">清除資料集限制</button><div class="content-scopes">${state.departments.map(department=>`<details open><summary>${esc(department.name)}</summary>${state.datasets.filter(d=>item(d.item_id)?.department_id===department.id).map(d=>`<label class="check"><input type="checkbox" data-content-dataset="${d.id}" ${s.datasets.includes(d.id)?'checked':''}>${esc(d.name)}${d.archived?'（封存）':''}</label>`).join('')}</details>`).join('')}</div><div id="content-semantic-opts" ${s.mode==='semantic'?'':'hidden'}><label for="content-minimum">最低相近度（排序分數）</label><input id="content-minimum" type="number" min="-1" max="1" step="0.01" value="${s.minimum}"><p class="muted">分數範圍 −1 至 1，越高越相近，並非辨識正確率。找不到照片時可嘗試同義詞或降低門檻。</p></div><div id="content-index-status" role="status">正在確認索引…</div><div class="row"><button id="content-status-refresh">更新狀態</button><button id="content-ocr-index-btn" ${s.mode==='ocr'?'':'hidden'}>建立 OCR 索引</button></div>`;
 $('#main').innerHTML=`<div class="toolbar"><h1>離線照片內容與文字搜尋</h1><div class="library-mode-switch" role="group" aria-label="搜尋模式"><button type="button" data-search-mode="semantic" aria-pressed="${s.mode==='semantic'}">AI 語意搜圖</button><button type="button" data-search-mode="ocr" aria-pressed="${s.mode==='ocr'}">OCR 文字搜圖</button></div><button id="content-show-on-map" hidden>在地圖圖台顯示這批結果</button></div><form id="content-form" class="card content-query"><label for="content-query">${s.mode==='ocr'?'搜尋照片中的英文或繁體中文文字（支援多關鍵字空白分隔）':'用中文描述照片內容'}</label><div class="row"><input id="content-query" value="${esc(s.query)}" maxlength="100" placeholder="${s.mode==='ocr'?'例如：115年度 堤防、水質監測、A00123':'例如：道路積水、交通錐、樹木倒塌'}" required><button class="primary" id="content-search-button">${s.mode==='ocr'?'搜尋圖片文字':'搜尋照片內容'}</button></div><p class="muted">${s.mode==='ocr'?'離線辨識照片中的告示牌、布條、財產編號等中英文文字，自動轉換繁體比對。':'以照片影像內容比對中文短句；不依檔名或人工標籤排序。請使用簡短關鍵字或短句。'}</p></form><div id="content-error" role="alert"></div><p id="content-summary" role="status">輸入關鍵字開始搜尋。</p><details class="card content-batch" open><summary>批量新增標籤</summary><div class="row"><button id="content-select-all">選取全部搜尋結果</button><button id="content-clear-selection">取消勾選</button><span id="content-selected-count">已選 0 張</span></div><label for="content-tags">新增人工標籤（逗號分隔）</label><input id="content-tags" placeholder="例如：道路巡查,待檢查" maxlength="1200"><p id="content-tag-preview" class="muted">先勾選照片，再填入標籤。</p><div class="row"><button id="content-tag-submit" class="primary">確認新增至勾選照片</button><button id="content-undo" ${s.lastBatch?'':'disabled'}>撤回上一批新增標籤</button></div><p class="muted">共享標籤需整理權限；封存資料集及唯讀照片仍可搜尋，但不能新增標籤。</p></details><div id="content-results" class="grid"></div>`;
 document.querySelectorAll('[data-search-mode]').forEach(b=>b.onclick=()=>{if(s.mode===b.dataset.searchMode)return;s.mode=b.dataset.searchMode;renderContentSearch()});
 function valid(){return epoch===s.epoch&&tab==='content'}
 function invalidate(){s.sequence++;s.rows=[];s.selected.clear();s.result=null;$('#content-results').innerHTML='';$('#content-summary').textContent='範圍或門檻已改變，請重新搜尋。';$('#content-error').textContent='';$('#content-search-button').disabled=false;selection();status()}
 function tags(){return [...new Set($('#content-tags').value.split(/[,，\n]/).map(t=>t.trim()).filter(Boolean))]}
 function selection(){
  $('#content-selected-count').textContent=`已選 ${s.selected.size} 張`;
  const labels=tags();$('#content-tag-preview').textContent=s.selected.size&&labels.length?`將為 ${s.selected.size} 張照片新增：${labels.join('、')}`:'先勾選照片，再填入標籤。';
  $('#content-tag-submit').disabled=s.tagBusy||!s.selected.size||!labels.length;$('#content-undo').disabled=s.tagBusy||!s.lastBatch;
 }
 async function status(){
  const seq=s.sequence;
  try{const r=await api('semantic/status',{datasets:s.datasets});if(!valid()||seq!==s.sequence)return;
   const info=s.mode==='ocr'?(r.ocr||{}):r;
   const label=s.mode==='ocr'?'離線 OCR 模型':'離線語意模型';
   $('#content-index-status').innerHTML=`<p>可閱覽 ${info.total??0} 張 · 已索引 ${info.indexed??0} 張<br>待索引 ${info.pending??0} 張 · 失敗 ${info.failed??0} 張</p><p class="muted">${info.ready?`${label}已就緒。`:`${label}尚未就緒，請確認環境安裝。`}${info.pending||info.failed?' 索引涵蓋尚未完整，零結果不代表沒有相關照片。':''}</p>`;
   if($('#content-ocr-index-btn'))$('#content-ocr-index-btn').disabled=!info.ready||!info.pending;
  }catch(e){if(valid()&&seq===s.sequence)$('#content-index-status').textContent=e.message}
 }
 function draw(){
  $('#content-results').innerHTML=s.rows.length?s.rows.map(p=>`<article class="photo content-result"><button class="content-photo-open" data-content-open="${p.id}" aria-label="開啟照片 ${esc(p.name)}"><img src="${p.preview}" alt="${esc(p.name)}" loading="lazy"></button><div class="caption"><label class="check"><input data-content-select="${p.id}" type="checkbox" ${s.selected.has(p.id)?'checked':''} ${!p.can_write||ds(p.dataset)?.archived?'disabled':''}><strong>${esc(p.name)}</strong></label><div class="muted">${esc(dep(p.department)?.name)}／${esc(ds(p.dataset)?.name)}</div>${s.mode==='ocr'?`<p class="muted">文字：${esc(p.ocr_snippet||'')}</p>`:`<p>內容相近度 ${p.semantic_score.toFixed(3)}</p>`}<p class="muted">${esc(compareDate(p.captured))}</p>${p.tags.map(t=>`<span class="badge">${esc(t)}</span>`).join('')}${!p.can_write||ds(p.dataset)?.archived?'<span class="badge">僅閱覽</span>':''}</div></article>`).join(''):'<div class="empty">目前沒有符合門檻的已索引照片。</div>';
  document.querySelectorAll('[data-content-select]').forEach(x=>x.onchange=()=>{x.checked?s.selected.add(+x.dataset.contentSelect):s.selected.delete(+x.dataset.contentSelect);selection()});
  document.querySelectorAll('[data-content-open]').forEach(b=>b.onclick=()=>action(async()=>{const r=await api('search',{});if(!valid())return;const p=r.photos.find(x=>x.id===+b.dataset.contentOpen);if(!p)throw Error('照片權限或狀態已改變，請重新搜尋');const fresh=new Map(state.photos.map(p=>[p.id,p]));fresh.set(p.id,p);state.photos=[...fresh.values()];openPhoto(p.id)}));
  if($('#content-show-on-map'))$('#content-show-on-map').hidden=!s.rows.length;
  selection();
 }
 if($('#content-show-on-map'))$('#content-show-on-map').onclick=()=>{if(!s.rows.length)return;libraryState.pinnedIds=s.rows.map(p=>p.id);libraryState.pinnedLabel=`${s.mode==='ocr'?'OCR 文字':'內容'}搜尋「${s.query}」`;libraryState.mode='map';libraryState.view=null;libraryState.group=null;tab='library';saveLibraryDisplay();render()};
 document.querySelectorAll('[data-content-dataset]').forEach(x=>x.onchange=()=>{s.datasets=[...document.querySelectorAll('[data-content-dataset]:checked')].map(x=>+x.dataset.contentDataset);invalidate()});
 $('#content-all-scope').onclick=()=>{s.datasets=[];document.querySelectorAll('[data-content-dataset]').forEach(x=>x.checked=false);invalidate()};
 $('#content-minimum').onchange=e=>{s.minimum=Number(e.target.value);invalidate()};
 $('#content-status-refresh').onclick=()=>action(status);
 if($('#content-ocr-index-btn'))$('#content-ocr-index-btn').onclick=()=>action(async()=>{const btn=$('#content-ocr-index-btn');btn.disabled=true;$('#content-summary').textContent='正在建立離線 OCR 文字索引…';try{const r=await api('semantic/ocr_index',{datasets:s.datasets});if(!valid())return;toast(`已處理 ${r.processed} 張照片的 OCR 文字索引`);await status()}finally{if(valid())btn.disabled=false}});
 $('#content-tags').oninput=selection;
 $('#content-select-all').onclick=()=>{s.selected=new Set(s.rows.filter(p=>p.can_write&&!ds(p.dataset)?.archived).map(p=>p.id));draw();toast(`已選取全部可整理搜尋結果 ${s.selected.size} 張；唯讀照片不納入`)};
 $('#content-clear-selection').onclick=()=>{s.selected.clear();draw()};
 $('#content-form').onsubmit=e=>{e.preventDefault();action(async()=>{
  s.query=$('#content-query').value.trim();const seq=++s.sequence;s.selected.clear();s.rows=[];s.result=null;$('#content-results').innerHTML='';selection();$('#content-error').textContent='';$('#content-summary').textContent=s.mode==='ocr'?'正在搜尋圖片中文字…':'正在以本機模型搜尋照片內容…';$('#content-search-button').disabled=true;
  try{
   const endpoint=s.mode==='ocr'?'semantic/ocr_search':'semantic/search';
   const payload=s.mode==='ocr'?{query:s.query,datasets:s.datasets,limit:500}:{query:s.query,datasets:s.datasets,minimum:s.minimum,limit:500};
   const r=await api(endpoint,payload);if(!valid()||seq!==s.sequence)return;
   s.rows=r.photos;s.result=r;const fresh=new Map(state.photos.map(p=>[p.id,p]));s.rows.forEach(p=>fresh.set(p.id,p));state.photos=[...fresh.values()];
   $('#content-summary').textContent=`「${s.query}」找到 ${r.matches} 張，顯示 ${r.returned} 張；已索引 ${r.indexed}／${r.total} 張${r.pending||r.failed?'（涵蓋尚未完整）':''}${r.limited?'。結果超過 500 張，請縮小資料集範圍或提高門檻；全部選取只包含目前這 500 張。':''}`;draw();await status();
  }catch(error){if(valid()&&seq===s.sequence){$('#content-error').textContent=error.message;$('#content-summary').textContent='搜尋未完成，請稍後重試。'}}finally{if(valid()&&seq===s.sequence)$('#content-search-button').disabled=false}
 })};
 $('#content-tag-submit').onclick=()=>action(async()=>{
  const ids=[...s.selected],labels=tags();if(s.tagBusy||!ids.length||!labels.length)return;s.tagBusy=true;selection();
  try{const r=await api('semantic/tag',{photos:ids,tags:labels});if(!valid())return;s.lastBatch=r.batch;$('#content-undo').disabled=false;for(const p of s.rows)if(ids.includes(p.id))p.tags=[...new Set([...p.tags,...labels])].sort();s.selected.clear();draw();toast(`已為 ${r.count} 張新增人工標籤，可撤回本批次`)}finally{s.tagBusy=false;if(valid())selection()}
 });
 $('#content-undo').onclick=()=>action(async()=>{if(s.tagBusy||!s.lastBatch)return;s.tagBusy=true;selection();try{await api('undo_tags',{batch:s.lastBatch});if(!valid())return;s.lastBatch=null;$('#content-undo').disabled=true;s.selected.clear();s.rows=[];s.sequence++;$('#content-results').innerHTML='';selection();$('#content-summary').textContent='已撤回本批次新增的標籤來源；其他來源保留。請重新搜尋更新結果。';toast('已撤回本批次標籤')}finally{s.tagBusy=false;if(valid())selection()}});
 selection();status();
}
