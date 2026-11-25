const collectionsUI={activeId:null,collections:[],items:[],total:0,offset:0,limit:100,hasNext:false,selected:new Set(),events:[],epoch:0};
function resetCollections(){collectionsUI.activeId=null;collectionsUI.collections=[];collectionsUI.items=[];collectionsUI.selected.clear();collectionsUI.events=[];collectionsUI.epoch++}
async function loadSavedSearchesPanel(){
 const wrap=$('#saved-searches-panel');if(!wrap)return;
 try{
  const r=await api('saved_searches/list',{});
  const rows=r.saved_searches||[];
  if(!$('#saved-searches-panel'))return;
  wrap.innerHTML=`<details class="filter-section" data-fold="saved" ${folds.saved?'':'open'}><summary>常用搜尋 <span class="filter-count">${rows.length||''}</span></summary><button type="button" id="save-current-search" style="width:100%;margin:8px 0">＋ 儲存目前搜尋條件</button><div class="saved-search-list">${rows.map(s=>`<div class="saved-search-row"><button type="button" data-apply-saved="${esc(s.id)}" class="saved-search-btn ${s.valid?'':'is-invalid'}">${esc(s.name)}${s.valid?'':' ⚠ 條件已失效'}</button><button type="button" data-del-saved="${esc(s.id)}" aria-label="刪除常用搜尋" class="saved-del-btn">✕</button></div>`).join('')||'<p class="muted">尚無儲存的常用搜尋。</p>'}</div></details>`;
  $('#save-current-search').onclick=()=>openSaveSearchDialog();
  wrap.querySelectorAll('[data-apply-saved]').forEach(b=>b.onclick=()=>action(async()=>{
   const id=b.dataset.applySaved;
   const r=await fetch('/api/saved_searches/apply',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify({id})});
   const j=await r.json();
   if(!r.ok){
    const msg=(j.warnings||[j.error||'部分條件已失效']).join('\n');
    openConfirmDialog({title:'常用搜尋條件已失效',message:`以下條件已失效或失去權限，系統不會自動放寬為全部範圍：\n${msg}`,confirmLabel:'知道了',onConfirm:async()=>{}});
    return;
   }
   const ss=j.saved_search,c=ss.criteria||{};
   let preloadedOpts=null;
   if((c.fields||[]).length||(c.datasets||[]).length===1){
    try{
     preloadedOpts=await api('filter_options',{departments:c.departments||[],datasets:c.datasets||[],status:c.status||'submitted',county:c.county||'',town:c.town||''});
    }catch(err){
     openConfirmDialog({title:'無法載入欄位定義',message:`載入此常用搜尋所需的欄位定義失敗（${err.message||'連線錯誤'}），為避免條件被靜默放寬，已取消套用。`,confirmLabel:'知道了',onConfirm:async()=>{}});
     return;
    }
    if((c.fields||[]).length){
     const defs=new Map((preloadedOpts.fields||[]).map(f=>[f.key+'|'+(f.type||'text'),f]));
     const missingRules=(c.fields||[]).filter(rule=>!defs.has(rule.key+'|'+rule.type));
     if(missingRules.length){
      openConfirmDialog({title:'常用搜尋欄位已失效',message:`以下自訂欄位定義已不存在，系統不會自動放寬為全部範圍：\n${missingRules.map(x=>x.key).join('、')}`,confirmLabel:'知道了',onConfirm:async()=>{}});
      return;
     }
    }
   }
   resetLibrary();
   filter.deps=c.departments||[];filter.datasets=c.datasets||[];filter.tag=(c.tags||[]).join(',');
   filter.text=c.text||'';filter.from=c.from||'';filter.to=c.to||'';filter.star=!!c.star;
   filter.status=c.status||'submitted';filter.county=c.county||'';filter.town=c.town||'';filter.village=c.village||'';
   libraryState.anomaly=c.anomaly||'';libraryState.mode=ss.display_mode==='wall'?'wall':'map';
   if(preloadedOpts){
    libraryState.filterOptions=preloadedOpts;
    libraryState.filterOptionsScope=currentFilterOptionsScopeKey();
   }
   for(const rule of (c.fields||[])){
    const key=rule.key+'|'+rule.type;
    if(rule.op==='missing')libraryState.fields[key]={missing:true};
    else if(rule.op==='contains')libraryState.fields[key]={value:rule.value};
    else if(rule.op==='range')libraryState.fields[key]={min:rule.min,max:rule.max};
    else if(rule.op==='in')libraryState.fields[key]={values:rule.values};
    else if(rule.op==='equals')libraryState.fields[key]={value:String(rule.value)};
   }
   saveLibraryDisplay();renderLibrary();toast(`已載入常用搜尋「${ss.name}」`);
  }));
  wrap.querySelectorAll('[data-del-saved]').forEach(b=>b.onclick=()=>action(async()=>{await api('saved_searches/delete',{id:b.dataset.delSaved});toast('已刪除常用搜尋');loadSavedSearchesPanel()}));
 }catch{}
}
function openSaveSearchDialog(){
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="儲存常用搜尋"><div class="toolbar"><h2>儲存常用搜尋</h2><button type="button" data-ux-close>關閉</button></div><p class="muted">僅儲存目前的搜尋條件與展示模式，重新開啟時依當時權限與資料重新計算。</p><form id="ux-save-search-form"><label for="ux-ss-name">常用搜尋名稱</label><input id="ux-ss-name" required maxlength="100" placeholder="例如：水利科本週待定位現場照"><div class="row" style="margin-top:14px"><button type="button" data-ux-close>取消</button><button type="submit" class="primary">儲存</button></div></form></section>`;
 document.body.append(m);$('#ux-ss-name').focus();
 m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
 $('#ux-save-search-form').onsubmit=e=>{e.preventDefault();action(async()=>{
  const criteria=currentLibraryCriteria(false);
  await api('saved_searches/save',{name:$('#ux-ss-name').value.trim(),display_mode:libraryState.mode,criteria});
  m.remove();toast('已儲存常用搜尋');loadSavedSearchesPanel();
 })};
}
function openAddToCollectionDialog(payloadData, count){
 if(!count)return toast('請先勾選要加入集合的照片');
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const openEpoch=selectionScopeEpoch;
 action(async()=>{
  const r=await api('collections/list',{});
  if(openEpoch!==selectionScopeEpoch){toast('選取範圍已變更，請重新確認選取結果');return}
  const editableCols=(r.collections||[]).filter(c=>c.can_manage);
  const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
  m.dataset.selectionDialog='1';
  m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="加入工作集合"><div class="toolbar"><h2>加入工作集合（已選 ${count} 張）</h2><button type="button" data-ux-close>關閉</button></div><p class="muted">集合僅保存照片關聯，不複製原圖或不改變所屬資料集；失權或回收後自動不可見。</p>${editableCols.length?`<form id="ux-col-add-form"><label for="ux-col-select">選擇工作集合</label><select id="ux-col-select">${editableCols.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}（${c.shared?esc(c.department_name)+' 共享':'個人'} · ${c.photo_count} 張）</option>`).join('')}</select><div class="row" style="margin-top:14px"><button type="button" data-ux-close>取消</button><button type="submit" class="primary">確認加入 ${count} 張</button></div></form>`:'<p class="metadata-warning">尚無可維護的工作集合，請先至「工作集合」分頁建立。</p>'}<div style="margin-top:10px"><button type="button" id="ux-goto-collections">前往工作集合管理</button></div></section>`;
  document.body.append(m);
  m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
  $('#ux-goto-collections').onclick=()=>{m.remove();tab='collections';render()};
  if($('#ux-col-add-form'))$('#ux-col-add-form').onsubmit=e=>{e.preventDefault();action(async()=>{
   if(openEpoch!==selectionScopeEpoch){m.remove();toast('選取範圍已變更，請重新確認選取結果');return}
   const res=await api('collections/add_items',{collection:$('#ux-col-select').value,...payloadData});
   m.remove();toast(`已新增 ${res.added} 張照片至工作集合「${res.collection.name}」`);
  })};
 });
}
function renderCollections(){
 const c=collectionsUI,epoch=++c.epoch;
 const writableDepts=state.departments.filter(d=>state.user.admin||state.user.capabilities?.[d.id]?.organize||(state.user.managed||[]).includes(d.id));
 $('#side').innerHTML=`<h3>人工工作集合</h3><p class="muted">跨時間檢閱、交接與整理；僅存照片關聯，不複製原圖且不擴大原權限。</p><form id="col-create-form" class="card" style="padding:12px"><h4>建立集合</h4><label for="col-name">集合名稱</label><input id="col-name" required maxlength="120" placeholder="例如：115Q1 護岸重點複查"><label for="col-desc">說明</label><textarea id="col-desc" rows="2" placeholder="用途或交接備註"></textarea><label for="col-dept">集合範圍</label><select id="col-dept"><option value="">個人集合（僅本人）</option>${writableDepts.map(d=>`<option value="${d.id}">${esc(d.name)} 共享集合</option>`).join('')}</select><button class="primary" style="width:100%;margin-top:10px">建立集合</button></form><div id="col-list" class="admin-nav"></div>`;
 $('#main').innerHTML=`<div class="toolbar"><h1>工作集合</h1><button id="col-refresh">重新整理</button></div><div id="col-detail"><div class="empty">請從左側選擇或建立工作集合。</div></div>`;
 $('#col-create-form').onsubmit=e=>{e.preventDefault();action(async()=>{
  const dept=$('#col-dept').value;
  const r=await api('collections/save',{name:$('#col-name').value.trim(),description:$('#col-desc').value.trim(),department:dept?+dept:null});
  c.activeId=r.collection.id;toast('工作集合已建立');await loadCollections();
 })};
 $('#col-refresh').onclick=()=>action(loadCollections);
 async function loadCollections(){
  const r=await api('collections/list',{});if(epoch!==c.epoch||tab!=='collections')return;
  c.collections=r.collections||[];
  if(!c.collections.some(x=>x.id===c.activeId))c.activeId=c.collections[0]?.id||null;
  $('#col-list').innerHTML=c.collections.map(x=>`<button type="button" data-col-id="${esc(x.id)}" class="${x.id===c.activeId?'primary':''}"><strong>${esc(x.name)}</strong><br><small>${x.shared?esc(x.department_name)+' 共享':'個人'} · ${x.photo_count} 張</small></button>`).join('')||'<p class="muted">尚無工作集合。</p>';
  document.querySelectorAll('[data-col-id]').forEach(b=>b.onclick=()=>{c.activeId=b.dataset.colId;c.offset=0;c.selected.clear();loadCollectionItems()});
  if(c.activeId)await loadCollectionItems();else $('#col-detail').innerHTML='<div class="empty">請從左側選擇或建立工作集合。</div>';
 }
 async function loadCollectionItems(){
  if(!c.activeId||epoch!==c.epoch||tab!=='collections')return;
  const r=await api('collections/items',{collection:c.activeId,limit:c.limit,offset:c.offset});
  if(epoch!==c.epoch||tab!=='collections')return;
  const col=r.collection;c.items=r.photos||[];c.total=r.total||0;c.hasNext=!!r.has_next;
  photoCacheUpsert(c.items);
  $('#col-detail').innerHTML=`<section class="card"><div class="toolbar"><div><h2>${esc(col.name)}</h2><p class="muted">${col.shared?esc(col.department_name)+' 共享集合':'個人集合'} · 建立者 ${esc(col.owner)} · 可閱覽 ${c.total} 張</p></div><div class="row" style="flex:0 0 auto">${col.can_manage?'<button type="button" id="col-edit-btn">編輯名稱／說明</button><button type="button" class="danger" id="col-del-btn">刪除集合</button>':''}</div></div>${col.description?`<p>${esc(col.description)}</p>`:''}<div class="row" style="margin-top:10px"><button type="button" id="col-select-all">選取本頁 (${c.items.length})</button><button type="button" id="col-clear-sel">取消勾選</button><button type="button" id="col-batch-star">批量打星</button><button type="button" id="col-batch-tag">批量標籤</button>${col.can_manage?'<button type="button" class="danger" id="col-remove-items">從集合移出已選</button>':''}</div><p class="muted" id="col-sel-count" style="margin-top:8px">已選 ${c.selected.size} 張（集合內操作皆依當下照片權限驗權）</p></section><div class="grid photo-wall" style="--photo-size:200px">${c.items.map(p=>`<article class="photo" data-col-open="${p.id}"><img loading="lazy" src="${p.preview}" alt="${esc(p.name)}"><div class="caption"><div style="float:right"><input type="checkbox" data-col-check="${p.id}" ${c.selected.has(p.id)?'checked':''}></div><strong>${p.star?'★ ':''}${esc(p.name)}</strong><div class="muted">${esc(ds(p.dataset)?.name)} · ${p.county?esc(p.county+p.town):''}</div></div></article>`).join('')||'<div class="empty">此集合目前沒有可閱覽的照片。</div>'}</div><details class="card" style="margin-top:14px"><summary>集合異動紀錄（${(r.events||[]).length}）</summary>${(r.events||[]).map(e=>`<p class="muted">${esc(displayCapture(e.created))} · ${esc(e.actor)} · ${esc(e.action)}</p>`).join('')||'<p class="muted">尚無紀錄</p>'}</details>`;
  document.querySelectorAll('[data-col-open]').forEach(el=>el.onclick=()=>openPhoto(+el.dataset.colOpen));
  document.querySelectorAll('[data-col-check]').forEach(cb=>cb.onclick=e=>{e.stopPropagation();const pid=+cb.dataset.colCheck;cb.checked?c.selected.add(pid):c.selected.delete(pid);$('#col-sel-count').textContent=`已選 ${c.selected.size} 張`});
  $('#col-select-all').onclick=()=>{c.items.forEach(p=>c.selected.add(p.id));loadCollectionItems()};
  $('#col-clear-sel').onclick=()=>{c.selected.clear();loadCollectionItems()};
  $('#col-batch-star').onclick=()=>{const ids=[...c.selected];if(!ids.length)return toast('請先勾選照片');action(async()=>{const res=await api('batch_star',{photos:ids});toast(`已新增 ${res.count} 顆星號`);await loadCollectionItems()})};
  $('#col-batch-tag').onclick=()=>{const ids=[...c.selected];if(!ids.length)return toast('請先勾選照片');openTagDialog({title:`集合批量新增標籤（${ids.length} 張）`,onConfirm:async tags=>{await api('batch_tags',{photos:ids,tags,batch:crypto.randomUUID()});toast('已完成集合批量標籤');await loadCollectionItems()}})};
  if($('#col-remove-items'))$('#col-remove-items').onclick=()=>{const ids=[...c.selected];if(!ids.length)return toast('請先勾選要移出的照片');action(async()=>{await api('collections/remove_items',{collection:col.id,photos:ids});c.selected.clear();toast('已從工作集合移出（原圖與資料集不變）');await loadCollections()})};
  if($('#col-del-btn'))$('#col-del-btn').onclick=()=>openConfirmDialog({title:'刪除工作集合',message:`確定刪除工作集合「${col.name}」？（不會刪除或移動任何照片原圖）`,confirmLabel:'確認刪除',onConfirm:async()=>{await api('collections/delete',{id:col.id});c.activeId=null;toast('工作集合已刪除');await loadCollections()}});
  if($('#col-edit-btn'))$('#col-edit-btn').onclick=()=>{
   document.querySelector('.ux-dialog-backdrop')?.remove();
   const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
   m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="編輯工作集合"><div class="toolbar"><h2>編輯工作集合</h2><button type="button" data-ux-close>關閉</button></div><form id="ux-col-edit"><label>集合名稱</label><input id="ux-col-name" required maxlength="120" value="${esc(col.name)}"><label>說明</label><textarea id="ux-col-desc">${esc(col.description||'')}</textarea><label>範圍</label><select id="ux-col-dept"><option value="">個人集合</option>${writableDepts.map(d=>`<option value="${d.id}" ${col.department===d.id?'selected':''}>${esc(d.name)} 共享集合</option>`).join('')}</select><div class="row" style="margin-top:12px"><button type="button" data-ux-close>取消</button><button type="submit" class="primary">儲存</button></div></form></section>`;
   document.body.append(m);m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
   $('#ux-col-edit').onsubmit=e=>{e.preventDefault();action(async()=>{const dv=$('#ux-col-dept').value;await api('collections/save',{id:col.id,name:$('#ux-col-name').value.trim(),description:$('#ux-col-desc').value.trim(),department:dv?+dv:null});m.remove();toast('已更新工作集合');await loadCollections()})};
  };
 }
 action(loadCollections);
}
