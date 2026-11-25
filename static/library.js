function libraryDisplayPreference(){try{return JSON.parse(localStorage.getItem('photo-library-display'))||{}}catch{return {}}}
const libraryDisplay=libraryDisplayPreference();
function saveLibraryDisplay(){try{localStorage.setItem('photo-library-display',JSON.stringify({mode:libraryState.mode,size:libraryState.size,sideCollapsed:libraryState.sideCollapsed,rightRatio:libraryState.rightRatio,stackBursts:libraryState.stackBursts}))}catch{}}
let libraryState={
 mode:libraryDisplay.mode==='wall'?'wall':'map',
 size:Math.max(112,Math.min(400,Number(libraryDisplay.size)||224)),
 sideCollapsed:!!libraryDisplay.sideCollapsed,
 rightRatio:Math.max(0.25,Math.min(0.50,Number(libraryDisplay.rightRatio)||0.34)),
 stackBursts:libraryDisplay.stackBursts!==false,
 ids:[],
 items:[],
 visible:[],
 view:null,
 follow:true,
 group:null,
 fields:{},
 anomaly:'',
 timer:null,
 clusterTimer:null,
 sequence:0,
 clusterSeq:0,
 summary:null,
 total:0,
 hasNext:false,
 nextOffset:null,
 selectionToken:'',
 loadingMore:false,
 loadError:'',
 filterOptions:null,
 pinnedIds:null,
 pinnedLabel:'',
 criteriaKey:'',
 displayCache:[],
 burstMap:new Map(),
 vCols:3,
 vRowH:310,
 vDisplayLen:0,
 measuredRowH:0,
 scrollRaf:0,
};
function resetLibrary(){
 clearTimeout(libraryState.timer);clearTimeout(libraryState.clusterTimer);
 libraryState.sequence++;libraryState.clusterSeq++;
 libraryState.ids=[];libraryState.items=[];libraryState.visible=[];libraryState.view=null;libraryState.group=null;
 libraryState.fields={};libraryState.anomaly='';libraryState.summary=null;libraryState.total=0;
 libraryState.hasNext=false;libraryState.nextOffset=null;libraryState.selectionToken='';
 libraryState.loadingMore=false;libraryState.loadError='';libraryState.filterOptions=null;
 libraryState.pinnedIds=null;libraryState.pinnedLabel='';libraryState.criteriaKey='';
 filter={deps:[],items:[],datasets:[],dataset:'',tag:'',text:'',from:'',to:'',star:false,status:'submitted',county:'',town:'',village:''};
 clearPhotoSelection();
}
function availableRegions(){
 const opts=libraryState.filterOptions||state?.filter_options||{counties:[],towns:[],villages:[]};
 return {counties:opts.counties||[],towns:opts.towns||[],villages:opts.villages||[]};
}
function libraryVisiblePhotos(){return libraryState.items}
function libraryFieldDefinitions(){
 if(filter.deps.length||filter.datasets.length!==1)return [];
 if(libraryState.filterOptions?.fields?.length)return libraryState.filterOptions.fields;
 const dataset=ds(filter.datasets[0]);if(!dataset)return [];
 return (item(dataset.item_id)?.fields||[]).map(f=>({...f,type:f.type||'text',historical:false}));
}
function libraryRules(){return libraryFieldDefinitions().flatMap(f=>{const r=libraryState.fields[f.key+'|'+f.type];if(!r)return [];if(r.missing)return [{key:f.key,type:f.type,op:'missing'}];if(['text','textarea'].includes(f.type)&&r.value)return [{key:f.key,type:f.type,op:'contains',value:r.value}];if(['integer','number','date'].includes(f.type)&&(r.min!==''&&r.min!=null||r.max!==''&&r.max!=null))return [{key:f.key,type:f.type,op:'range',min:r.min??'',max:r.max??''}];if(['select','multi'].includes(f.type)&&r.values?.length)return [{key:f.key,type:f.type,op:'in',values:r.values}];if(f.type==='boolean'&&['true','false'].includes(r.value))return [{key:f.key,type:f.type,op:'equals',value:r.value==='true'}];return []})}
function currentLibraryCriteria(includeMapScope=true){
 const ls=libraryState;
 const c={
  departments:filter.deps,
  datasets:filter.datasets,
  tags:selectedTags(),
  text:filter.text,
  from:filter.from,
  to:filter.to,
  star:filter.star,
  status:filter.status,
  anomaly:ls.anomaly,
  county:filter.county,
  town:filter.town,
  village:filter.village,
  fields:libraryRules(),
 };
 if(ls.pinnedIds)c.ids=ls.pinnedIds;
 if(includeMapScope&&ls.mode==='map'&&map){
  if(ls.group)c.cluster=ls.group;
  else if(ls.follow){
   const b=map.getBounds();
   c.bounds={north:b.getNorth(),south:b.getSouth(),east:b.getEast(),west:b.getWest()};
  }
 }
 return c;
}
async function fetchMatchedBurstGroup(burstGroup){
 if(!burstGroup)return null;
 const c={...currentLibraryCriteria(true),burst_group:burstGroup,offset:0,limit:200};
 const r=await api('search',c);
 photoCacheUpsert(r.photos||[]);
 return r.photos||[];
}
async function refreshLibraryFilterOptions(){
 const seq=libraryState.sequence;
 try{
  const opts=await api('filter_options',{departments:filter.deps,datasets:filter.datasets,status:filter.status,county:filter.county,town:filter.town});
  if(seq!==libraryState.sequence||tab!=='library')return;
  libraryState.filterOptions=opts;
  drawLibraryRegionControls();
  drawLibraryTagChips();
  drawLibraryFieldFilters();
 }catch{}
}
function drawLibraryRegionControls(){
 const regions=availableRegions();
 if($('#fcounty'))$('#fcounty').innerHTML=`<option value="">全部縣市</option><option value="__missing__" ${filter.county==='__missing__'?'selected':''}>尚無行政區</option>${regions.counties.map(c=>`<option value="${esc(c)}" ${filter.county===c?'selected':''}>${esc(c)}</option>`).join('')}`;
 if($('#ftown')){$('#ftown').innerHTML=`<option value="">全部鄉鎮市區</option>${regions.towns.map(t=>`<option value="${esc(t)}" ${filter.town===t?'selected':''}>${esc(t)}</option>`).join('')}`;$('#ftown').disabled=filter.county==='__missing__'}
 if($('#fvillage')){$('#fvillage').innerHTML=`<option value="">全部村里</option>${regions.villages.map(v=>`<option value="${esc(v)}" ${filter.village===v?'selected':''}>${esc(v)}</option>`).join('')}`;$('#fvillage').disabled=filter.county==='__missing__'}
}
function drawLibraryTagChips(){
 const wrap=$('#lib-tag-chips');if(!wrap)return;
 wrap.innerHTML=availableTags().map(t=>`<button type="button" data-lib-tag="${esc(t)}" class="tag-chip ${selectedTags().includes(t)?'is-selected':''}" aria-pressed="${selectedTags().includes(t)}">${selectedTags().includes(t)?'✓ ':''}${esc(t)}</button>`).join('')||'<span class="muted">此範圍沒有標籤</span>';
 wrap.querySelectorAll('[data-lib-tag]').forEach(b=>b.onclick=()=>{libraryState.pinnedIds=null;libraryState.pinnedLabel='';const tags=selectedTags();filter.tag=(tags.includes(b.dataset.libTag)?tags.filter(t=>t!==b.dataset.libTag):[...tags,b.dataset.libTag]).join(',');libraryState.group=null;renderLibrary()});
}
function renderLibrary(){
 const ls=libraryState;if(map){ls.view={center:map.getCenter(),zoom:map.getZoom()};map.remove();map=null}clearTimeout(ls.timer);clearTimeout(ls.clusterTimer);
 const layoutEl=document.querySelector('.layout');
 if(layoutEl)layoutEl.classList.toggle('is-side-collapsed',ls.sideCollapsed);
 const regions=availableRegions();
 $('#side').innerHTML=`<div class="side-header"><h3>搜尋與篩選</h3><button type="button" id="toggle-side-collapse" aria-label="收折搜尋與篩選">◀ 收折</button></div><div class="filters"><label for="search">檔案名稱</label><input id="search" value="${esc(filter.text)}" placeholder="搜尋檔案名稱"><details class="filter-section" data-fold="tree" ${folds.tree?'':'open'}><summary>科室與資料集</summary>${state.departments.map(d=>`<details class="department-node" data-fold="dep-${d.id}" ${folds['dep-'+d.id]?'':'open'}><summary>${esc(d.name)}<span class="filter-count">${state.datasets.filter(x=>item(x.item_id)?.department_id===d.id).length}</span></summary><label class="tree-all"><input type="checkbox" data-lib-dept="${d.id}" ${filter.deps.includes(d.id)?'checked':''}>全部${esc(d.name)}照片</label><div class="dataset-nodes">${state.datasets.filter(x=>item(x.item_id)?.department_id===d.id).map(x=>`<label><input type="checkbox" data-lib-dataset="${x.id}" ${filter.datasets.includes(x.id)?'checked':''}>${esc(x.name)}${x.archived?'<small>封存</small>':''}</label>`).join('')||'<span class="muted">尚無資料集</span>'}</div></details>`).join('')}</details>
 <details class="filter-section" data-fold="region" ${folds.region?'':'open'}><summary>行政區（自動反查）<span class="filter-count">${filter.county?1:0||''}</span></summary><label for="fcounty">縣市</label><select id="fcounty"><option value="">全部縣市</option><option value="__missing__" ${filter.county==='__missing__'?'selected':''}>尚無行政區</option>${regions.counties.map(c=>`<option value="${esc(c)}" ${filter.county===c?'selected':''}>${esc(c)}</option>`).join('')}</select><label for="ftown">鄉鎮市區</label><select id="ftown" ${filter.county==='__missing__'?'disabled':''}><option value="">全部鄉鎮市區</option>${regions.towns.map(t=>`<option value="${esc(t)}" ${filter.town===t?'selected':''}>${esc(t)}</option>`).join('')}</select><label for="fvillage">村里</label><select id="fvillage" ${filter.county==='__missing__'?'disabled':''}><option value="">全部村里</option>${regions.villages.map(v=>`<option value="${esc(v)}" ${filter.village===v?'selected':''}>${esc(v)}</option>`).join('')}</select><div id="region-stats-chips" class="tag-chips" style="margin-top:10px"></div></details>
 <details class="filter-section" data-fold="tags" ${folds.tags?'':'open'}><summary>標籤 <span class="filter-count">${selectedTags().length||''}</span></summary><div class="tag-filter-tools"><input type="search" id="tagsearch" aria-label="尋找標籤" placeholder="尋找標籤"><button id="cleartags">清除</button></div><div class="tag-chips" id="lib-tag-chips"></div><p class="muted">可複選，同類條件任一符合。</p></details>
 <label for="ffrom">拍攝起日（臺灣時間）</label><input type="date" id="ffrom" value="${filter.from}"><label for="fto">拍攝迄日（含當天）</label><input type="date" id="fto" value="${filter.to}"><label class="check"><input type="checkbox" id="fstar" ${filter.star?'checked':''}>只看我的星號</label>
 <label for="fanomaly">人工異常狀態</label><select id="fanomaly">${[['','全部照片'],['any','有異常紀錄'],['none','沒有異常紀錄'],['pending','待確認'],['confirmed','確認異常'],['dismissed','已排除']].map(([v,l])=>`<option value="${v}" ${ls.anomaly===v?'selected':''}>${l}</option>`).join('')}</select><label for="fstatus">照片狀態</label><select id="fstatus">${[['submitted','正式照片'],['draft','可閱覽草稿'],['recycled','回收區（需管理權）']].map(([v,l])=>`<option value="${v}" ${filter.status===v?'selected':''}>${l}</option>`).join('')}</select>
 <details class="filter-section" data-fold="custom" ${folds.custom?'':'open'}><summary>自訂表單欄位 <span class="filter-count">${libraryRules().length||''}</span></summary><div id="custom-field-filters"></div></details><div id="saved-searches-panel"></div><button id="clear" style="margin-top:15px">清除全部篩選</button></div>`;
 $('#main').innerHTML=`<div class="toolbar library-toolbar">${ls.sideCollapsed?'<button type="button" id="toggle-side-expand">▶ 展開篩選</button>':''}<div class="library-title-group"><h1>${ls.mode==='wall'?'照片圖牆':'照片圖台'}</h1><p class="muted" id="search-summary" role="status">搜尋中…</p></div><div class="library-mode-switch" role="group" aria-label="照片展示模式"><button type="button" data-library-mode="map" aria-pressed="${ls.mode==='map'}">地理圖台</button><button type="button" data-library-mode="wall" aria-pressed="${ls.mode==='wall'}">照片圖牆</button></div><label class="check burst-toggle" title="將連拍且影像相似的照片自動收合為一疊"><input type="checkbox" id="toggle-burst-stack" ${ls.stackBursts?'checked':''}>堆疊連拍</label><div class="library-actions"><button id="selectall">選取全部符合結果</button><button id="clearselection">取消選取</button><button id="batchstar">批量打星</button><button id="batchtag">批量標籤</button><button id="addtocollection">加入工作集合</button><button id="undo-lib-tags" ${lastTagBatch?'':'disabled'}>撤回本次標籤</button>${state.user.admin||Object.values(state.user.capabilities||{}).some(p=>p.export)?'<button id="export-library">匯出已選照片</button>':''}</div></div>${ls.pinnedIds?`<div class="pinned-banner" role="status"><span>目前顯示「${esc(ls.pinnedLabel)}」共 ${ls.pinnedIds.length} 張結果（已暫時覆蓋左側篩選）。</span><button type="button" id="clear-pinned">返回一般篩選</button></div>`:''}<div id="search-error" role="alert"></div><div class="library-split-workspace ${ls.mode==='map'?'is-map-split':'is-wall-mode'}" style="--right-pane-vw:${(ls.rightRatio*100).toFixed(2)}"><section id="library-map-panel" ${ls.mode==='map'?'':'hidden'}><div class="map-note">底圖：內政部國土測繪中心 WMTS（右上角可切換電子地圖／衛星正射影像／離線網格）。</div><div id="map"></div><div class="map-result-controls"><label class="check"><input type="checkbox" id="follow" ${ls.follow?'checked':''}>只顯示目前地圖範圍（地圖與清單同步）</label><button id="fit-results">縮放至搜尋結果</button><button id="reset-cluster" ${ls.group?'':'hidden'}>返回全部搜尋結果</button></div></section><div id="library-splitter" class="library-splitter" role="separator" aria-orientation="vertical" aria-label="調整圖台與照片清單比例" tabindex="0" ${ls.mode==='map'?'':'hidden'}><span>⋮</span></div><section class="library-results-pane" id="library-results-pane"><div class="results-pane-header"><p id="map-result-summary" class="muted"></p><p class="selected-count">已選 ${selectedPhotoCount()} 張</p></div><div class="grid ${ls.mode==='wall'?'photo-wall':''}" style="--photo-size:${ls.size}px" id="cards"><div class="empty">正在更新搜尋結果…</div></div><div class="results-pane-footer"><div class="wall-size-controls"><button type="button" id="wall-smaller" aria-label="縮小縮圖">－</button><label for="wall-size">卡片縮放</label><input id="wall-size" type="range" min="112" max="400" step="16" value="${ls.size}"><button type="button" id="wall-larger" aria-label="放大縮圖">＋</button><output id="wall-size-value" for="wall-size">${ls.size} px</output></div></div></section></div>`;
 const toggleSide=()=>{ls.sideCollapsed=!ls.sideCollapsed;saveLibraryDisplay();renderLibrary()};
 if($('#toggle-side-collapse'))$('#toggle-side-collapse').onclick=toggleSide;
 if($('#toggle-side-expand'))$('#toggle-side-expand').onclick=toggleSide;
 const splitter=$('#library-splitter'),splitWrap=$('.library-split-workspace');
 if(splitter&&splitWrap){
  const applyRatio=ratio=>{ls.rightRatio=Math.max(0.25,Math.min(0.50,ratio));splitWrap.style.setProperty('--right-pane-vw',(ls.rightRatio*100).toFixed(2));if(map)map.invalidateSize()};
  let dragging=false;
  splitter.onpointerdown=e=>{if(e.button!==0)return;e.preventDefault();dragging=true;splitter.setPointerCapture(e.pointerId);splitter.classList.add('is-dragging')};
  splitter.onpointermove=e=>{if(!dragging)return;const splitX=e.clientX/window.innerWidth;applyRatio(1-splitX)};
  const endDrag=()=>{if(!dragging)return;dragging=false;splitter.classList.remove('is-dragging');saveLibraryDisplay();if(map)map.invalidateSize()};
  splitter.onpointerup=splitter.onpointercancel=splitter.onlostpointercapture=endDrag;
  splitter.onkeydown=e=>{if(e.key==='ArrowLeft'){e.preventDefault();applyRatio(ls.rightRatio+0.02);saveLibraryDisplay()}else if(e.key==='ArrowRight'){e.preventDefault();applyRatio(ls.rightRatio-0.02);saveLibraryDisplay()}};
 }
 document.querySelectorAll('[data-library-mode]').forEach(button=>button.onclick=()=>{if(ls.mode===button.dataset.libraryMode)return;ls.mode=button.dataset.libraryMode;ls.group=null;saveLibraryDisplay();renderLibrary()});
 $('#toggle-burst-stack').onchange=e=>{ls.stackBursts=e.target.checked;saveLibraryDisplay();drawLibraryResults()};
 $('#wall-size').oninput=e=>setLibraryPhotoSize(+e.target.value);
 $('#wall-smaller').onclick=()=>setLibraryPhotoSize(ls.size-32);$('#wall-larger').onclick=()=>setLibraryPhotoSize(ls.size+32);
 const cardsEl=$('#cards');
 cardsEl.addEventListener('wheel',e=>{if(!(e.ctrlKey||e.metaKey))return;e.preventDefault();setLibraryPhotoSize(ls.size+(e.deltaY<0?16:-16))},{passive:false});
 cardsEl.addEventListener('scroll',()=>{
  if(ls.scrollRaf)return;
  ls.scrollRaf=requestAnimationFrame(()=>{
   ls.scrollRaf=0;
   drawLibraryResults(false);
   if(ls.hasNext&&!ls.loadingMore&&!ls.loadError&&cardsEl.scrollTop+cardsEl.clientHeight>=cardsEl.scrollHeight-Math.max(400,(ls.vRowH||300)*2)){
    loadNextLibraryPage();
   }
  });
 });
 $('#search').oninput=e=>{filter.text=e.target.value;scheduleLibrarySearch()};for(const [id,key] of [['ffrom','from'],['fto','to']])$('#'+id).onchange=e=>{filter[key]=e.target.value;scheduleLibrarySearch(0)};
 $('#fstar').onchange=e=>{filter.star=e.target.checked;scheduleLibrarySearch(0)};$('#fanomaly').onchange=e=>{ls.anomaly=e.target.value;scheduleLibrarySearch(0)};$('#fstatus').onchange=e=>{ls.pinnedIds=null;ls.pinnedLabel='';filter.status=e.target.value;ls.fields={};ls.group=null;renderLibrary()};
 $('#fcounty').onchange=e=>{ls.pinnedIds=null;ls.pinnedLabel='';filter.county=e.target.value;filter.town='';filter.village='';renderLibrary()};
 $('#ftown').onchange=e=>{ls.pinnedIds=null;ls.pinnedLabel='';filter.town=e.target.value;filter.village='';renderLibrary()};
 $('#fvillage').onchange=e=>{ls.pinnedIds=null;ls.pinnedLabel='';filter.village=e.target.value;renderLibrary()};
 document.querySelectorAll('[data-lib-dept]').forEach(x=>x.onchange=()=>{ls.pinnedIds=null;ls.pinnedLabel='';filter.deps=[...document.querySelectorAll('[data-lib-dept]:checked')].map(y=>+y.dataset.libDept);ls.fields={};ls.group=null;renderLibrary()});
 document.querySelectorAll('[data-lib-dataset]').forEach(x=>x.onchange=()=>{ls.pinnedIds=null;ls.pinnedLabel='';filter.datasets=[...document.querySelectorAll('[data-lib-dataset]:checked')].map(y=>+y.dataset.libDataset);ls.fields={};ls.group=null;renderLibrary()});
 $('#tagsearch').oninput=e=>document.querySelectorAll('[data-lib-tag]').forEach(b=>b.hidden=!b.dataset.libTag.toLocaleLowerCase().includes(e.target.value.toLocaleLowerCase()));$('#cleartags').onclick=()=>{ls.pinnedIds=null;ls.pinnedLabel='';filter.tag='';renderLibrary()};
 document.querySelectorAll('[data-fold]').forEach(x=>x.addEventListener('toggle',()=>{folds[x.dataset.fold]=!x.open;localStorage.setItem('photo-filter-folds',JSON.stringify(folds))}));
 $('#clear').onclick=()=>{const view=ls.view,follow=ls.follow;resetLibrary();ls.view=view;ls.follow=follow;renderLibrary()};
 if($('#clear-pinned'))$('#clear-pinned').onclick=()=>{ls.pinnedIds=null;ls.pinnedLabel='';ls.view=null;renderLibrary()};
 $('#follow').onchange=e=>{ls.follow=e.target.checked;ls.group=null;scheduleLibrarySearch(0,true)};
 $('#fit-results').onclick=fitLibraryResults;
 $('#reset-cluster').onclick=()=>{ls.group=null;scheduleLibrarySearch(0,true)};
 $('#selectall').onclick=()=>{
  if(!ls.total||!ls.selectionToken)return toast('目前沒有可選取的搜尋結果');
  clearPhotoSelection();
  selectionAll={token:ls.selectionToken,total:ls.total,excluded:new Set()};
  drawLibraryResults(false);
  toast(`已跨頁選取全部符合條件的結果共 ${ls.total} 張（含連拍堆疊組內符合照片）`);
 };
 $('#clearselection').onclick=()=>{clearPhotoSelection();drawLibraryResults(false)};
 $('#batchstar').onclick=()=>{
  const count=selectedPhotoCount();if(!count)return toast('請先勾選照片');
  const payloadData=selectionPayload();
  openConfirmDialog({title:'確認批量打星',message:`已選取 ${count} 張照片（跨頁全選最多處理 500 張），將為其中尚未打星的項目加上個人星號，確定執行？`,confirmLabel:`打星 ${count} 張`,onConfirm:async()=>{const res=await api('batch_star',payloadData);await refresh();toast(`已為 ${res.count} 張新照片加上星號（共檢核 ${res.matched??count} 張）`)}});
 };
 $('#batchtag').onclick=()=>{
  const count=selectedPhotoCount();if(!count)return toast('請先勾選照片');
  const payloadData=selectionPayload();
  openTagDialog({title:`批量新增標籤（共 ${count} 張）`,hint:`將為實際已選取的 ${count} 張照片（跨頁全選上限 500 張）新增以下標籤。`,onConfirm:async tags=>{const batch=crypto.randomUUID();const res=await api('batch_tags',{...payloadData,tags,batch});lastTagBatch=batch;await refresh();toast(`已為 ${res.count} 張照片新增標籤`)}});
 };
 $('#undo-lib-tags').onclick=()=>action(async()=>{await api('undo_tags',{batch:lastTagBatch});lastTagBatch=null;await refresh()});
 $('#addtocollection').onclick=()=>{if(typeof openAddToCollectionDialog==='function')openAddToCollectionDialog(selectionPayload(),selectedPhotoCount())};
 if($('#export-library'))$('#export-library').onclick=()=>{
  const count=selectedPhotoCount();if(!count)return toast('請先勾選要匯出的照片');
  const payloadData=selectionPayload();
  const useBackground=!!selectionAll||count>20;
  openConfirmDialog({
   title:'確認匯出照片',
   message:useBackground?`即將排入背景工作中心打包匯出 ${count} 張正式照片與中繼資料 CSV（背景執行不中斷，完成後於工作中心下載），確定匯出？`:`即將打包匯出實際已選取的 ${count} 張正式照片與中繼資料 CSV，確定匯出？`,
   confirmLabel:`匯出 ${count} 張`,
   onConfirm:async()=>{
    if(useBackground){
     const r=await api('jobs/create',{kind:'export',...payloadData});
     toast(`已排入背景匯出工作（${r.job.id.slice(0,8)}），請至「工作中心」查看進度與下載`);
     if(state.summary)state.summary.active_jobs=(state.summary.active_jobs||0)+1;
    }else{
     const result=await api('export',payloadData);
     const link=document.createElement('a');link.href=result.url;link.textContent='下載匯出 ZIP';
     $('#export-library').after(link);toast(`已完成 ${count} 張照片匯出`);
    }
   }
  });
 };
 drawLibraryTagChips();
 setLibraryPhotoSize(ls.size, false);
 drawLibraryFieldFilters();
 if(ls.mode==='map')initializeLibraryMap();
 if(typeof loadSavedSearchesPanel==='function')loadSavedSearchesPanel();
 refreshLibraryFilterOptions();
 scheduleLibrarySearch(0,true);
}
function drawLibraryFieldFilters(){
 const definitions=libraryFieldDefinitions();if(!definitions.length){$('#custom-field-filters').innerHTML='<p class="muted">請只勾選一個資料集，顯示其目前與歷史表單欄位。</p>';return}
 $('#custom-field-filters').innerHTML=definitions.map((f,index)=>{const id=f.key+'|'+f.type,r=libraryState.fields[id]||{},prefix='custom-'+index;let control='';if(['text','textarea'].includes(f.type))control=`<input id="${prefix}" data-custom-id="${esc(id)}" data-custom-part="value" value="${esc(r.value||'')}" placeholder="包含文字">`;else if(['integer','number','date'].includes(f.type))control=`<div class="custom-range"><input aria-label="${esc(f.label)} 起值" type="${f.type==='date'?'date':'number'}" step="${f.type==='integer'?'1':'any'}" data-custom-id="${esc(id)}" data-custom-part="min" value="${esc(r.min??'')}" placeholder="最小值"><span>至</span><input aria-label="${esc(f.label)} 迄值" type="${f.type==='date'?'date':'number'}" step="${f.type==='integer'?'1':'any'}" data-custom-id="${esc(id)}" data-custom-part="max" value="${esc(r.max??'')}" placeholder="最大值"></div>`;else if(f.type==='boolean')control=`<select id="${prefix}" data-custom-id="${esc(id)}" data-custom-part="value"><option value="">全部</option><option value="true" ${r.value==='true'?'selected':''}>是</option><option value="false" ${r.value==='false'?'selected':''}>否</option></select>`;else control=`<div class="custom-options">${(f.options||[]).map(value=>`<button class="tag-chip ${r.values?.includes(value)?'is-selected':''}" data-custom-choice="${esc(id)}" data-choice="${esc(value)}" aria-pressed="${!!r.values?.includes(value)}">${esc(value)}</button>`).join('')}</div>`;return `<div class="custom-filter"><label for="${prefix}">${esc(f.label)} ${f.historical?'<small>歷史欄位</small>':''}</label>${control}<label class="check small"><input type="checkbox" data-custom-missing="${esc(id)}" ${r.missing?'checked':''}>只看未填／無此欄位</label></div>`}).join('');
 document.querySelectorAll('[data-custom-id]').forEach(input=>input.oninput=()=>{const id=input.dataset.customId;libraryState.fields[id]={...libraryState.fields[id],[input.dataset.customPart]:input.value,missing:false};document.querySelectorAll('[data-custom-missing]').forEach(x=>{if(x.dataset.customMissing===id)x.checked=false});scheduleLibrarySearch()});
 document.querySelectorAll('[data-custom-choice]').forEach(b=>b.onclick=()=>{const id=b.dataset.customChoice,r=libraryState.fields[id]||{},values=r.values||[];libraryState.fields[id]={...r,values:values.includes(b.dataset.choice)?values.filter(x=>x!==b.dataset.choice):[...values,b.dataset.choice],missing:false};drawLibraryFieldFilters();scheduleLibrarySearch(0)});
 document.querySelectorAll('[data-custom-missing]').forEach(input=>input.onchange=()=>{const id=input.dataset.customMissing;libraryState.fields[id]={...libraryState.fields[id],missing:input.checked};scheduleLibrarySearch(0)});
}
function currentCriteriaKey(){return JSON.stringify({deps:filter.deps,datasets:filter.datasets,tags:selectedTags(),text:filter.text,from:filter.from,to:filter.to,star:filter.star,status:filter.status,anomaly:libraryState.anomaly,county:filter.county,town:filter.town,village:filter.village,fields:libraryRules(),pinned:libraryState.pinnedIds||null})}
function scheduleLibrarySearch(delay=200,preservePinned=false){
 if(!preservePinned){libraryState.pinnedIds=null;libraryState.pinnedLabel=''}
 const nextKey=currentCriteriaKey();
 if(libraryState.criteriaKey&&libraryState.criteriaKey!==nextKey){clearPhotoSelection();libraryState.group=null}
 libraryState.criteriaKey=nextKey;
 clearTimeout(libraryState.timer);
 libraryState.sequence++;
 const sequence=libraryState.sequence;
 if($('#search-summary'))$('#search-summary').textContent='正在更新篩選…';
 const summary=document.querySelector('[data-fold=custom]>summary');
 if(summary)summary.innerHTML='自訂表單欄位 <span class="filter-count">'+(libraryRules().length||'')+'</span>';
 if($('#cards'))$('#cards').setAttribute('aria-busy','true');
 libraryState.timer=setTimeout(()=>runLibrarySearch(sequence),delay);
}
async function runLibrarySearch(sequence){
 const ls=libraryState;
 ls.loadingMore=false;ls.loadError='';
 try{
  const payload=currentLibraryCriteria(true);
  payload.limit=100;payload.offset=0;
  const result=await api('search',payload);
  if(sequence!==ls.sequence||tab!=='library')return;
  ls.items=result.photos||[];
  ls.ids=result.ids||[];
  ls.summary=result;
  ls.total=result.total||0;
  ls.hasNext=!!result.has_next;
  ls.nextOffset=result.next_offset;
  ls.selectionToken=result.selection_token||'';
  if(selectionAll)selectionAll.token=ls.selectionToken;
  photoCacheUpsert(ls.items);
  if($('#cards'))$('#cards').scrollTop=0;
  $('#search-error').textContent='';
  $('#search-summary').textContent=`共 ${result.total} 張 · ${result.located} 張有座標 · ${result.missing_location} 張待補位置${result.excluded_missing_time?' · 時間條件排除 '+result.excluded_missing_time+' 張無時間照片':''}`;
  const chips=$('#region-stats-chips');
  if(chips){chips.innerHTML=(result.region_stats||[]).slice(0,12).map(r=>`<button type="button" class="tag-chip ${filter.county===r.county&&filter.town===r.town?'is-selected':''}" data-region-stat="${esc(r.county)}|${esc(r.town)}">${esc(r.county)}${esc(r.town)} (${r.count})</button>`).join('');chips.querySelectorAll('[data-region-stat]').forEach(b=>b.onclick=()=>{const [c,t]=b.dataset.regionStat.split('|');if(filter.county===c&&filter.town===t){filter.county='';filter.town='';filter.village=''}else{filter.county=c;filter.town=t;filter.village=''}ls.pinnedIds=null;ls.pinnedLabel='';renderLibrary()})}
  if(map&&!ls.view&&result.located)await fitLibraryResults();
  drawLibraryResults(true);
 }catch(e){
  if(sequence!==ls.sequence||tab!=='library')return;
  ls.ids=[];ls.items=[];ls.summary=null;ls.total=0;ls.hasNext=false;ls.nextOffset=null;ls.selectionToken='';
  $('#search-error').textContent=e.message;$('#search-error').className='metadata-warning';$('#search-summary').textContent='搜尋條件需要調整';
  drawLibraryResults(false);
 }finally{
  if(sequence===ls.sequence&&$('#cards'))$('#cards').removeAttribute('aria-busy');
 }
}
async function loadNextLibraryPage(){
 const ls=libraryState,seq=ls.sequence;
 if(ls.loadingMore||!ls.hasNext||ls.nextOffset==null)return;
 ls.loadingMore=true;ls.loadError='';drawLibraryResults(false);
 try{
  const payload=currentLibraryCriteria(true);
  payload.limit=100;payload.offset=ls.nextOffset;
  const result=await api('search',payload);
  if(seq!==ls.sequence||tab!=='library')return;
  const existing=new Set(ls.items.map(p=>p.id));
  const added=(result.photos||[]).filter(p=>!existing.has(p.id));
  ls.items=[...ls.items,...added];
  ls.ids=ls.items.map(p=>p.id);
  ls.hasNext=!!result.has_next;
  ls.nextOffset=result.next_offset;
  if(result.selection_token)ls.selectionToken=result.selection_token;
  photoCacheUpsert(added);
 }catch(e){
  if(seq!==ls.sequence||tab!=='library')return;
  ls.loadError=e.message||'載入失敗';
 }finally{
  if(seq===ls.sequence){ls.loadingMore=false;drawLibraryResults(false)}
 }
}
let libraryMarkers;
function initializeLibraryMap(){
 if(!window.L){$('#map').textContent='地圖元件未就緒，清單仍可搜尋';return}
 map=L.map('map').setView(libraryState.view?.center||[25.033,121.565],libraryState.view?.zoom||11);
 attachBaseMaps(map);libraryMarkers=L.layerGroup().addTo(map);
 map.on('moveend',()=>{
  libraryState.view={center:map.getCenter(),zoom:map.getZoom()};
  refreshMapClusters();
  if(libraryState.follow&&!libraryState.group)scheduleLibrarySearch(150,true);
 });
}
async function refreshMapClusters(){
 if(!map||!libraryMarkers||libraryState.mode!=='map'||tab!=='library')return;
 const seq=++libraryState.clusterSeq;
 const b=map.getBounds();
 const payload={...currentLibraryCriteria(false),bounds:{north:b.getNorth(),south:b.getSouth(),east:b.getEast(),west:b.getWest()},zoom:map.getZoom(),grid_px:55};
 try{
  const res=await api('map_clusters',payload);
  if(seq!==libraryState.clusterSeq||!map||!libraryMarkers||tab!=='library')return;
  libraryMarkers.clearLayers();
  for(const c of res.clusters||[]){
   const activeGroup=libraryState.group&&libraryState.group.bx===c.bx&&libraryState.group.by===c.by&&libraryState.group.zoom===c.zoom;
   L.marker([c.lat,c.lon],{icon:L.divIcon({html:String(c.count),className:`cluster ${activeGroup?'is-active-cluster':''}`,iconSize:[36,36]}),title:c.count===1?(c.single_photo_name||'單張照片'):`群聚 ${c.count} 張`}).addTo(libraryMarkers).on('click',()=>{
    if(c.count===1&&c.single_photo_id){openPhoto(c.single_photo_id);return}
    libraryState.group={zoom:c.zoom,bx:c.bx,by:c.by,grid_px:c.grid_px,count:c.count};
    scheduleLibrarySearch(0,true);
    toast(`已限定此群聚 ${c.count} 張；可點「返回全部搜尋結果」`);
   });
  }
 }catch{}
}
async function fitLibraryResults(){
 if(!map)return;
 try{
  const b=await api('map_clusters',{...currentLibraryCriteria(false),zoom:6,grid_px:80});
  const pts=(b.clusters||[]).map(c=>[c.lat,c.lon]);
  if(pts.length){map.fitBounds(pts,{maxZoom:17,padding:[35,35]});libraryState.view={center:map.getCenter(),zoom:map.getZoom()}}
  else toast('此搜尋結果沒有可定位的照片');
 }catch{}
}
function drawLibraryResults(refreshClusters=true){
 if(tab!=='library'||!$('#cards'))return;
 const ls=libraryState,rows=ls.items||[];
 ls.visible=rows;
 $('.selected-count').textContent=`已選 ${selectedPhotoCount()} 張`;
 if($('#reset-cluster'))$('#reset-cluster').hidden=!ls.group;
 $('#map-result-summary').textContent=ls.mode==='wall'?`圖牆顯示已載入 ${rows.length}／符合共 ${ls.total} 張（含待補位置） · 虛擬捲動渲染 · 拖曳或 Ctrl＋滾輪縮放。`:`目前清單已載入 ${rows.length}／符合共 ${ls.total} 張${ls.follow?'（地圖範圍內）':'（全部搜尋範圍）'}${ls.group?' · 已限定群聚 ('+ls.group.count+' 張)':''}；有座標照片由後端聚合於地圖。`;
 let footerHtml='';
 if(ls.loadingMore)footerHtml='<div class="vscroll-status">正在載入下一頁照片（每頁 100 張）…</div>';
 else if(ls.loadError)footerHtml=`<div class="metadata-warning vscroll-status">載入後續照片失敗：${esc(ls.loadError)} <button type="button" id="lib-retry-page">重試載入</button></div>`;
 else if(ls.hasNext)footerHtml=`<div class="vscroll-status"><button type="button" id="lib-load-more">載入更多照片（目前 ${rows.length}／${ls.total} 張）</button></div>`;
 drawCards(rows,{
  virtualize:true,
  footerHtml,
  onAfterDraw:()=>{
   if($('#lib-load-more'))$('#lib-load-more').onclick=()=>loadNextLibraryPage();
   if($('#lib-retry-page'))$('#lib-retry-page').onclick=()=>loadNextLibraryPage();
  },
 });
 if(refreshClusters&&ls.mode==='map'&&map)refreshMapClusters();
}
function setLibraryPhotoSize(value,redraw=true){
 const ls=libraryState,cards=$('#cards');
 let anchorIdx=0;
 if(redraw&&cards&&ls.vCols){
  const cardsTop=cards.getBoundingClientRect().top;
  const visibleCards=[...cards.querySelectorAll('.photo[data-vidx]')];
  const anchor=visibleCards.find(el=>el.getBoundingClientRect().bottom>cardsTop)||visibleCards[0];
  if(anchor)anchorIdx=Number(anchor.dataset.vidx)||0;
 }
 ls.size=Math.max(112,Math.min(400,Math.round((value-112)/16)*16+112));
 ls.measuredRowH=0;
 if(cards)cards.style.setProperty('--photo-size',ls.size+'px');
 if($('#wall-size'))$('#wall-size').value=ls.size;
 if($('#wall-size-value'))$('#wall-size-value').textContent=ls.size+' px';
 if($('#wall-smaller'))$('#wall-smaller').disabled=ls.size<=112;
 if($('#wall-larger'))$('#wall-larger').disabled=ls.size>=400;
 saveLibraryDisplay();
 if(redraw&&cards){
  drawLibraryResults(false);
  const actualRowH=ls.measuredRowH||(Math.round(ls.size*0.72)+148);
  const newCols=ls.vCols||Math.max(1,Math.floor((Math.max(200,cards.clientWidth||800)+14)/(ls.size+14)));
  cards.scrollTop=Math.floor(anchorIdx/newCols)*actualRowH;
  drawLibraryResults(false);
 }
}
