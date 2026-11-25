
let folds={};try{folds=JSON.parse(localStorage.getItem('photo-filter-folds')||'{}')}catch{}
function selectedTags(){return filter.tag?filter.tag.split(',').map(t=>t.trim()).filter(Boolean):[]}
function scopeMatches(p){return (!filter.deps.length&&!filter.datasets.length)||filter.deps.includes(p.department)||filter.datasets.includes(p.dataset)}
function availableTags(){
 const base=(typeof libraryState!=='undefined'&&libraryState.filterOptions?.tags)||state.filter_options?.tags||[];
 return [...new Set([...base,...selectedTags()])].sort((a,b)=>a.localeCompare(b,'zh-TW'));
}
function photoCacheUpsert(list){
 if(!state)return;
 const m=new Map((state.photos||[]).map(p=>[p.id,p]));
 for(const p of list||[])if(p&&p.id!=null){m.delete(p.id);m.set(p.id,p)}
 const arr=[...m.values()];
 state.photos=arr.length>500?arr.slice(arr.length-500):arr;
}
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let state,tab='library',selected=new Set(),filter={deps:[],items:[],datasets:[],dataset:'',tag:'',text:'',from:'',to:'',star:false,status:'submitted',county:'',town:'',village:''},current,map,compare=false;const app=$('#app');function toast(s){$('#toast').textContent=s;$('#toast').style.display='block';setTimeout(()=>$('#toast').style.display='none',4500)}async function api(path,data){let r=await fetch('/api/'+path,{method:data?'POST':'GET',headers:data instanceof FormData?{'X-CSRFToken':csrf()}:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:data instanceof FormData?data:data?JSON.stringify(data):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||'操作失敗');return j}function csrf(){return document.cookie.split('; ').find(x=>x.startsWith((document.querySelector('meta[name=csrf-cookie]')?.content||'csrftoken')+'='))?.split('=')[1]||''}async function action(fn){try{await fn()}catch(e){toast(e.message)}}async function refresh(){const prevPhotos=state?.photos||[];state=await api('state');state.photos=prevPhotos;render()}function ds(id){return state.datasets.find(x=>x.id==id)}function item(id){return state.items.find(x=>x.id==id)}function dep(id){return state.departments.find(x=>x.id==id)}function option(list,value,key='name'){return list.map(x=>`<option value="${x.id}" ${x.id==value?'selected':''}>${esc(x[key])}</option>`).join('')}function loginView(){app.innerHTML=`<div class="login"><div class="badge">LOCAL WORKSPACE</div><h1>機關影像庫</h1><p class="muted">內網照片管理 · 本機開發版</p><form id="login"><label>帳號</label><input name="username" required autocomplete="username"><label>密碼</label><input name="password" type="password" required autocomplete="current-password"><button class="primary" style="width:100%;margin-top:22px">登入系統</button></form><p class="muted">測試帳號請見專案 README。資料保存在本機。</p></div>`;$('#login').onsubmit=e=>{e.preventDefault();action(async()=>{await api('login',Object.fromEntries(new FormData(e.target)));await refresh()})}}
function render(){if(map){map.remove();map=null}const caps=Object.values(state.user.capabilities||{});const canUpload=state.user.admin||(state.user.writable||[]).length>0;const canAnalyze=state.user.admin||caps.some(c=>c.analyze);const canPeople=caps.some(c=>c.people_view);const canAdmin=state.user.admin||(state.user.managed||[]).length>0||caps.some(c=>c.forms);const pendingCount=(state.summary?.todo_requests||0)+(state.summary?.unread_notices||0);const activeJobs=state.summary?.active_jobs||0;const tabs=[['library','照片圖台',true],['content','內容搜尋',true],['similarity','近似分析',canAnalyze],['people','人物名冊',canPeople],['external','外部上傳',canUpload],['upload','上傳照片',canUpload],['jobs','工作中心',true],['inbox','案件與通知',true],['admin','管理後台',canAdmin]].filter(([,,ok])=>ok);if(!tabs.some(([id])=>id===tab))tab='library';document.querySelector('.layout')?.classList.remove('is-side-collapsed');app.innerHTML=`<header><div><b>機關影像庫</b><br><small>PHOTO MANAGEMENT / LOCAL</small></div><nav>${tabs.map(([id,n])=>`<button type="button" data-tab="${id}" class="${tab==id?'active':''}">${n}${id==='inbox'&&pendingCount?` <span class="nav-badge">${pendingCount}</span>`:''}${id==='jobs'&&activeJobs?` <span class="nav-badge">${activeJobs}</span>`:''}</button>`).join('')}</nav><span class="user">${esc(state.user.name)} ${state.user.admin?'· 系統管理者':''}</span><button type="button" id="logout">登出</button></header><div class="layout"><aside id="side"></aside><main class="main" id="main"></main></div>`;const navEl=$('#app header nav');if(navEl)navEl.onclick=e=>{const b=e.target.closest('[data-tab]');if(!b)return;if(formDirty&&tab==='admin'&&b.dataset.tab!=='admin'&&!confirm('尚有未發布的表單修改，確定離開？'))return;tab=b.dataset.tab;render()};$('#logout').onclick=()=>action(async()=>{await api('logout',{});editor=null;formDirty=false;adminSection='forms';resetUpload();resetLibrary();resetContentSearch();resetPeople();resetExternal();if(typeof resetJobs==='function')resetJobs();loginView()});if(tab==='library')library();if(tab==='similarity')renderSimilarity();if(tab==='content')renderContentSearch();if(tab==='people')renderPeople();if(tab==='external')renderExternal();if(tab==='upload')upload();if(tab==='jobs'&&typeof renderJobs==='function')renderJobs();if(tab==='inbox')inbox();if(tab==='admin')admin()}
function photos(){return libraryVisiblePhotos()}
function library(){renderLibrary()}

let selectionAll=null;
function isPhotoSelected(id){return selectionAll?!selectionAll.excluded.has(id):selected.has(id)}
function selectedPhotoCount(){return selectionAll?Math.max(0,selectionAll.total-selectionAll.excluded.size):selected.size}
function setPhotoSelected(id,checked){if(selectionAll){if(checked)selectionAll.excluded.delete(id);else selectionAll.excluded.add(id)}else{if(checked)selected.add(id);else selected.delete(id)}}
function clearPhotoSelection(){selected.clear();selectionAll=null}
function selectionPayload(explicitList=null){if(selectionAll&&!explicitList)return {selection_token:selectionAll.token,excluded_photos:[...selectionAll.excluded]};return {photos:explicitList||[...selected]}}
function drawCards(list, options={}){
 const container=$('#cards');if(!container)return;
 const stackActive=typeof libraryState!=='undefined'&&libraryState.stackBursts&&filter.status==='submitted';
 const burstMap=new Map(),display=[];
 if(stackActive){
  const groups=new Map();
  for(const p of list){
   if(!p.burst_group)continue;
   if(!groups.has(p.burst_group))groups.set(p.burst_group,[]);
   groups.get(p.burst_group).push(p);
  }
  const seen=new Set();
  for(const p of list){
   const g=p.burst_group?groups.get(p.burst_group):null;
   if(g&&g.length>1){
    if(seen.has(p.burst_group))continue;
    seen.add(p.burst_group);
    const cover=g.find(x=>x.burst_cover)||g[0];
    burstMap.set(cover.id,g);
    display.push(cover);
   }else{display.push(p)}
  }
 }else{display.push(...list)}
 const matchedByGroup=typeof libraryState!=='undefined'?(libraryState.summary?.burst_matched_counts||{}):{};
 if(!display.length){
  container.innerHTML=options.emptyHtml||'<div class="empty">此範圍尚無照片。可先到上傳頁加入照片，或調整篩選條件。</div>';
  return;
 }
 if(typeof libraryState!=='undefined'&&tab==='library'){libraryState.displayCache=display;libraryState.burstMap=burstMap}
 const virtualized=!!options.virtualize&&typeof libraryState!=='undefined';
 let startIdx=0,endIdx=display.length,topSpacer=0,bottomSpacer=0;
 if(virtualized){
  const ls=libraryState,size=ls.size||224,gap=14,width=Math.max(200,container.clientWidth||800);
  const cols=Math.max(1,Math.floor((width+gap)/(size+gap)));
  const rowH=ls.measuredRowH||(Math.round(size*0.72)+148);
  const totalRows=Math.ceil(display.length/cols);
  const scrollTop=container.scrollTop||0,viewH=container.clientHeight||640;
  const startRow=Math.max(0,Math.floor(scrollTop/rowH)-2);
  const endRow=Math.min(totalRows,startRow+Math.ceil(viewH/rowH)+5);
  startIdx=startRow*cols;endIdx=Math.min(display.length,endRow*cols);
  topSpacer=startRow*rowH;bottomSpacer=Math.max(0,(totalRows-endRow)*rowH);
  ls.vCols=cols;ls.vRowH=rowH;ls.vDisplayLen=display.length;
 }
 const slice=display.slice(startIdx,endIdx);
 const cardsHtml=slice.map((p,idx)=>{
  const dispIdx=startIdx+idx;
  const g=burstMap.get(p.id);
  const matchedCount=Math.max(g?g.length:1,p.burst_group?(matchedByGroup[p.burst_group]||1):1);
  const totalCount=p.burst_group?Math.max(matchedCount,p.burst_total_count||matchedCount):1;
  const partial=p.burst_group&&totalCount>1&&matchedCount<totalCount;
  const isStack=!!g||matchedCount>1;
  const selectedInGroup=g?g.filter(x=>isPhotoSelected(x.id)).length:(isPhotoSelected(p.id)?1:0);
  const allGroupSelected=g&&selectedInGroup===g.length;
  const badgeText=isStack?(partial?`${p.burst_cover?'★ 精選 · ':''}符合 ${matchedCount} / 共 ${totalCount} 張`:`${p.burst_cover?'★ 精選 · ':''}連拍 ${totalCount} 張`):'';
  return `<article class="photo ${isStack?'is-burst-stack':''}" data-open="${p.id}" data-vidx="${dispIdx}">${isStack?`<button type="button" class="burst-badge" data-burst-open="${p.id}" title="展開檢視與設為精選首圖">${badgeText}</button>`:''}<div class="photo-img-wrap is-loading"><img loading="lazy" src="${p.preview}" alt="${esc(p.name)}"><button type="button" class="img-retry-btn" hidden>載入失敗 · 重試</button></div><div class="caption"><div style="float:right"><input type="checkbox" data-select="${p.id}" ${isPhotoSelected(p.id)?'checked':''} title="${isStack?'選取這張（僅封面）':'選取這張'}" aria-label="${isStack?'選取這張（僅封面）':'選取'} ${esc(p.name)}"></div><strong>${p.star?'★ ':''}${esc(p.name)}</strong><div class="muted">${esc(ds(p.dataset)?.name)} · ${p.captured?esc(new Date(p.captured).toLocaleString('zh-TW')):'待補拍攝時間'}</div>${!isStack&&partial?`<span class="badge warn">符合 ${matchedCount} / 共 ${totalCount} 張</span>`:''}${p.county?`<span class="badge">${esc(p.county)}${esc(p.town)}</span>`:''}${p.tags.map(t=>`<span class="badge">${esc(t)}</span>`).join('')}${p.lat==null?'<span class="badge warn">待補定位</span>':''}${p.anomalies.length?'<span class="badge warn">異常紀錄</span>':''}${isStack?`<div class="burst-card-actions"><span class="muted">已選 ${selectedInGroup}/${matchedCount} 張</span><button type="button" data-select-group="${p.id}">${allGroupSelected&&matchedCount===(g?.length||1)?'取消整組':'選取整組 ('+matchedCount+')'}</button></div>`:''}</div></article>`;
 }).join('');
 container.innerHTML=(topSpacer?`<div class="vscroll-spacer" style="grid-column:1/-1;height:${topSpacer}px" aria-hidden="true"></div>`:'')+cardsHtml+(bottomSpacer?`<div class="vscroll-spacer" style="grid-column:1/-1;height:${bottomSpacer}px" aria-hidden="true"></div>`:'')+(options.footerHtml||'');
 if(virtualized){
  const firstCard=container.querySelector('.photo');
  if(firstCard&&firstCard.offsetHeight>80)libraryState.measuredRowH=firstCard.offsetHeight+14;
 }
 container.querySelectorAll('.photo-img-wrap img').forEach(img=>{
  const wrap=img.closest('.photo-img-wrap'),retryBtn=wrap?.querySelector('.img-retry-btn');
  if(img.complete&&img.naturalWidth>0){wrap?.classList.remove('is-loading');return}
  img.onload=()=>{wrap?.classList.remove('is-loading','is-error');if(retryBtn)retryBtn.hidden=true};
  img.onerror=()=>{wrap?.classList.remove('is-loading');wrap?.classList.add('is-error');if(retryBtn)retryBtn.hidden=false};
  if(retryBtn)retryBtn.onclick=e=>{e.stopPropagation();wrap?.classList.remove('is-error');wrap?.classList.add('is-loading');retryBtn.hidden=true;img.src=img.src.split('?')[0]+'?r='+Date.now()};
 });
 container.querySelectorAll('[data-open]').forEach(x=>x.onclick=()=>{const pid=+x.dataset.open,g=burstMap.get(pid);if(g)openBurstModal(g);else openPhoto(pid)});
 container.querySelectorAll('[data-burst-open]').forEach(b=>b.onclick=e=>{e.stopPropagation();const pid=+b.dataset.burstOpen,g=burstMap.get(pid);if(g)openBurstModal(g);else{const ph=display.find(item=>item.id===pid);if(ph)openBurstModal([ph])}});
 container.querySelectorAll('[data-select]').forEach(x=>x.onclick=e=>{e.stopPropagation();setPhotoSelected(+x.dataset.select,x.checked);if(typeof drawLibraryResults==='function'&&tab==='library')drawLibraryResults();else $('.selected-count').textContent=`已選 ${selectedPhotoCount()} 張`});
 container.querySelectorAll('[data-select-group]').forEach(b=>b.onclick=e=>{
  e.stopPropagation();const pid=+b.dataset.selectGroup,ph=display.find(item=>item.id===pid),g=burstMap.get(pid)||(ph?[ph]:null);if(!g)return;
  const gid=g[0].burst_group,matchedCount=gid?(matchedByGroup[gid]||g.length):g.length;
  action(async()=>{
   let targetGroup=g;
   if(gid&&matchedCount>g.length&&typeof fetchMatchedBurstGroup==='function'){targetGroup=await fetchMatchedBurstGroup(gid)||g}
   const allSelected=targetGroup.every(x=>isPhotoSelected(x.id));
   for(const x of targetGroup)setPhotoSelected(x.id,!allSelected);
   if(typeof drawLibraryResults==='function'&&tab==='library')drawLibraryResults();
   toast(allSelected?`已取消選取該連拍組 ${targetGroup.length} 張`:`已選取該連拍組符合條件的 ${targetGroup.length} 張`);
  });
 });
 if(options.onAfterDraw)options.onAfterDraw();
}
function openBurstModal(group){
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 let sorted=[...group].sort((a,b)=>(a.captured||'').localeCompare(b.captured||'')||a.id-b.id);
 const gid=sorted[0]?.burst_group;
 const matchedCount=gid&&typeof libraryState!=='undefined'?(libraryState.summary?.burst_matched_counts?.[gid]||sorted.length):sorted.length;
 const totalCount=Math.max(sorted.length,matchedCount,sorted[0]?.burst_total_count||sorted.length);
 function renderModal(){
  const selectedInGroup=sorted.filter(x=>isPhotoSelected(x.id)).length;
  const allSelected=selectedInGroup===sorted.length;
  m.innerHTML=`<section class="card ux-dialog ux-burst-dialog" role="dialog" aria-modal="true" aria-label="連拍照片組"><div class="toolbar"><h2>連拍照片組（目前符合 ${sorted.length}／可閱覽共 ${totalCount} 張）</h2><button type="button" id="burst-modal-select-group">${allSelected?'取消整組選取':'選取整組符合條件 ('+sorted.length+' 張)'}</button><button type="button" data-ux-close>關閉 Esc</button></div><p class="muted">系統依拍攝時間差與影像指紋自動收合；僅對目前符合搜尋條件的 ${sorted.length} 張進行選取。已選 ${selectedInGroup} 張。</p><div class="burst-grid">${sorted.map(p=>`<article class="burst-item ${p.burst_cover?'is-cover':''}"><img src="${p.preview}" alt="${esc(p.name)}" loading="lazy"><div class="burst-item-info"><label class="check" style="margin:0 0 6px"><input type="checkbox" data-burst-select="${p.id}" ${isPhotoSelected(p.id)?'checked':''}><strong>${esc(p.name)}</strong></label><span class="muted">${p.captured?esc(new Date(p.captured).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})):'無拍攝時間'}</span>${p.burst_cover?`<span class="badge">★ ${p.burst_cover_manual?'人工精選首圖':'目前封面首圖'}</span>`:''}<div class="row" style="margin-top:8px"><button type="button" data-burst-view="${p.id}">檢視大圖</button>${(p.can_write&&!ds(p.dataset)?.archived&&!p.burst_cover)?`<button type="button" class="primary" data-burst-cover="${p.id}">★ 設為精選首圖</button>`:''}</div></div></article>`).join('')}</div></section>`;
  m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
  $('#burst-modal-select-group').onclick=()=>{for(const x of sorted)setPhotoSelected(x.id,!allSelected);renderModal();if(typeof drawLibraryResults==='function'&&tab==='library')drawLibraryResults()};
  m.querySelectorAll('[data-burst-select]').forEach(cb=>cb.onchange=()=>{setPhotoSelected(+cb.dataset.burstSelect,cb.checked);renderModal();if(typeof drawLibraryResults==='function'&&tab==='library')drawLibraryResults()});
  m.querySelectorAll('[data-burst-view]').forEach(b=>b.onclick=()=>openPhoto(+b.dataset.burstView));
  m.querySelectorAll('[data-burst-cover]').forEach(b=>b.onclick=()=>action(async()=>{
   const pid=+b.dataset.burstCover;
   await api('burst_cover',{photo:pid});
   for(const ph of state.photos||[])if(ph.burst_group===gid){ph.burst_cover=(ph.id===pid);ph.burst_cover_manual=(ph.id===pid)}
   if(typeof libraryState!=='undefined')for(const ph of libraryState.items||[])if(ph.burst_group===gid){ph.burst_cover=(ph.id===pid);ph.burst_cover_manual=(ph.id===pid)}
   for(const ph of sorted){ph.burst_cover=(ph.id===pid);ph.burst_cover_manual=(ph.id===pid)}
   renderModal();
   if(typeof drawLibraryResults==='function')drawLibraryResults();
   toast('已更新連拍組的精選首圖');
  }));
 }
 document.body.append(m);
 renderModal();
 if(gid&&matchedCount>sorted.length&&typeof fetchMatchedBurstGroup==='function'){
  fetchMatchedBurstGroup(gid).then(full=>{if(full&&full.length&&m.isConnected){sorted=full.sort((a,b)=>(a.captured||'').localeCompare(b.captured||'')||a.id-b.id);renderModal()}}).catch(()=>{});
 }
}
function openConfirmDialog({title,message,confirmLabel='確認執行',onConfirm}){
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="toolbar"><h2>${esc(title)}</h2><button type="button" data-ux-close>關閉</button></div><p>${esc(message)}</p><div class="row" style="margin-top:16px"><button type="button" data-ux-close>取消</button><button type="button" id="ux-confirm-ok" class="primary">${esc(confirmLabel)}</button></div></section>`;
 document.body.append(m);
 const ok=$('#ux-confirm-ok');ok.focus();
 m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
 ok.onclick=()=>action(async()=>{ok.disabled=true;try{await onConfirm();m.remove()}finally{if(ok.isConnected)ok.disabled=false}});
}
function attachBaseMaps(targetMap){
 const Grid=L.GridLayer.extend({createTile(c){const t=document.createElement('canvas');t.width=t.height=256;const ctx=t.getContext('2d');ctx.fillStyle='#eaf0ec';ctx.fillRect(0,0,256,256);ctx.strokeStyle='#cbd8d1';ctx.strokeRect(0,0,256,256);ctx.fillStyle='#8da097';ctx.font='11px sans-serif';ctx.fillText(`Z${c.z} / ${c.x} / ${c.y}`,12,22);return t}});
 const offlineGrid=new Grid();
 const attr='© 內政部國土測繪中心';
 const nlscEmap=L.tileLayer('https://wmts.nlsc.gov.tw/wmts/EMAP/default/EPSG:3857/{z}/{y}/{x}',{maxZoom:20,maxNativeZoom:18,attribution:attr});
 const nlscPhoto=L.tileLayer('https://wmts.nlsc.gov.tw/wmts/PHOTO2/default/EPSG:3857/{z}/{y}/{x}',{maxZoom:20,maxNativeZoom:19,attribution:attr});
 const nlscPhotoBase=L.tileLayer('https://wmts.nlsc.gov.tw/wmts/PHOTO2/default/EPSG:3857/{z}/{y}/{x}',{maxZoom:20,maxNativeZoom:19,attribution:attr});
 const nlscPhotoMix=L.tileLayer('https://wmts.nlsc.gov.tw/wmts/PHOTO_MIX/default/EPSG:3857/{z}/{y}/{x}',{maxZoom:20,maxNativeZoom:19,attribution:attr});
 const nlscHybrid=L.layerGroup([nlscPhotoBase,nlscPhotoMix]);
 nlscEmap.addTo(targetMap);
 L.control.layers({'通用電子地圖':nlscEmap,'衛星／正射影像（含路網地名）':nlscHybrid,'衛星／正射影像（純圖）':nlscPhoto,'離線座標網格':offlineGrid},null,{position:'topright'}).addTo(targetMap);
}
function drawMap(list){if(!window.L){$('#map').innerHTML='<p>Leaflet 尚未就緒，請重新安裝前端依賴。</p>';return}map=L.map('map').setView([25.033,121.565],11);attachBaseMaps(map);let pts=list.filter(p=>p.lat!=null);if(pts.length)map.fitBounds(pts.map(p=>[p.lat,p.lon]),{maxZoom:16,padding:[30,30]});let layer=L.layerGroup().addTo(map);function markers(){layer.clearLayers();const groups=new Map();for(let p of pts){let pos=map.project([p.lat,p.lon]),key=Math.floor(pos.x/55)+':'+Math.floor(pos.y/55);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(p)}for(let group of groups.values()){let p=group[0];L.marker([p.lat,p.lon],{icon:L.divIcon({html:String(group.length),className:'cluster',iconSize:[36,36]})}).addTo(layer).on('click',()=>{if(group.length===1)openPhoto(p.id);else{drawCards(group);toast(`此群聚 ${group.length} 張，已顯示於下方清單`)}})}if($('#follow')?.checked)drawCards(list.filter(p=>p.lat!=null&&map.getBounds().contains([p.lat,p.lon])))}map.on('moveend',markers);$('#follow').onchange=()=>{$('#follow').checked?markers():drawCards(list)};markers()}
function upload(){renderUpload()}

function fields(p){return p.fields.map(f=>{let value=p.attrs[f.key]??'',name='attr_'+f.key;return `<label>${esc(f.label)}${f.required?' *':''}</label>`+(f.type==='multi'?`<select multiple name="${esc(name)}">${(f.options||[]).map(v=>`<option ${Array.isArray(value)&&value.includes(v)?'selected':''}>${esc(v)}</option>`).join('')}</select>`:f.type==='select'?`<select name="${esc(name)}"><option value="">請選擇</option>${(f.options||[]).map(v=>`<option ${value===v?'selected':''}>${esc(v)}</option>`).join('')}</select>`:f.type==='boolean'?`<select name="${esc(name)}"><option value="">未填</option><option value="true" ${value===true?'selected':''}>是</option><option value="false" ${value===false?'selected':''}>否</option></select>`:f.type==='textarea'?`<textarea name="${esc(name)}">${esc(value)}</textarea>`:`<input name="${esc(name)}" type="${['integer','number'].includes(f.type)?'number':f.type==='date'?'date':'text'}" step="${f.type==='integer'?'1':'any'}" value="${esc(value)}">`)}).join('')}
function payload(p,form){let fd=new FormData(form),attrs={};for(let f of p.fields){let v=f.type==='multi'?fd.getAll('attr_'+f.key):fd.get('attr_'+f.key);attrs[f.key]=f.type==='boolean'?(v===''?null:v==='true'):v}return {photo:p.id,version:p.version,captured:fd.get('captured'),time_confirmed:fd.has('time_confirmed'),lat:fd.get('lat'),lon:fd.get('lon'),attrs}}
async function openPhoto(id){
 const res=await api('photo_detail',{photo:id});
 const p=res.photo;photoCacheUpsert([p]);current=p;compare=false;document.querySelector('.modal')?.remove();let m=document.createElement('div');m.className='modal';m.innerHTML=`<div class="bar"><strong>${esc(p.name)}</strong>${p.can_download?'<button id="toggle-original" aria-pressed="false">載入原圖檢視</button>':''}<button id="toggleprop">屬性（空白鍵／右鍵）</button><button id="compare">比對附近照片</button><button id="close">關閉 Esc</button></div><div class="viewer"><div class="image-area" id="single"><img src="${p.preview}" draggable="false" alt="${esc(p.name)}"></div><aside class="properties" id="props"><h3>照片屬性</h3><p class="muted">${esc(dep(p.department)?.name)} / ${esc(ds(p.dataset)?.name)}</p><span class="badge">${esc(p.status)}</span><span class="badge">表單 v${p.form_version}</span><form id="meta"><label>拍攝時間（臺灣時間）</label><input type="datetime-local" name="captured" step="1" value="${p.captured?new Date(new Date(p.captured).getTime()+8*3600000).toISOString().slice(0,19):''}"><label class="check"><input type="checkbox" name="time_confirmed" ${p.time_confirmed?'checked':''}>已確認時間與時區</label><div class="row"><div><label>緯度</label><input type="number" step="any" name="lat" value="${p.lat??''}"></div><div><label>經度</label><input type="number" step="any" name="lon" value="${p.lon??''}"></div></div>${p.county?`<p class="muted" style="margin:6px 0">行政區（自動反查）：<strong>${esc(p.county)} ${esc(p.town)} ${esc(p.village)}</strong></p>`:''}${(p.status==='draft'||(p.status==='submitted'&&p.can_write))?'<button type="button" id="pick-photo-location">地圖點選定位</button>':''}${fields(p)}${p.status==='draft'?'<button type="button" id="save">儲存草稿</button><button type="button" class="primary" id="submit">正式提交</button><button type="button" id="discard">放棄草稿</button>':p.status==='submitted'&&p.can_write?'<button type="button" id="correct">申請上述資料更正</button>':''}</form><hr><button id="star">${p.star?'★ 取消收藏':'☆ 打星收藏'}</button><button id="addtag">新增標籤</button>${p.can_write?'<button id="anomaly">人工異常標註</button>':''}${p.status==='submitted'&&p.can_write?'<button id="delete" class="danger">申請刪除</button><button id="move">申請搬移</button>':''}${p.status==='recycled'&&p.can_manage?'<button id="restore">復原照片</button>':''}<p>${p.tags.map(t=>`<span class="badge">${esc(t)}</span>`).join('')}</p><a href="${p.original}" style="color:#135c58">下載原始照片（需權限）</a><h4>人工異常</h4>${p.anomalies.map(a=>`<p>${esc(a.text)} <span class="badge warn">${esc(a.state)}</span><br>${esc(a.resolution)} ${p.can_manage?`<button data-resolve="${a.id}">處理／重開</button>`:''}</p>`).join('')||'<p class="muted">尚無異常標註</p>'}</aside></div>`;document.body.append(m);$('#close').onclick=()=>m.remove();if($('#toggle-original'))$('#toggle-original').onclick=e=>{const img=$('#single img'),orig=e.target.getAttribute('aria-pressed')!=='true';img.src=orig?p.original:p.preview;e.target.setAttribute('aria-pressed',orig);e.target.textContent=orig?'切回預覽圖':'載入原圖檢視';toast(orig?'已切換為高解析原圖（細節檢視模式）':'已切回標準預覽圖')};$('#toggleprop').onclick=()=>$('#props').classList.toggle('hidden');$('#single').oncontextmenu=e=>{e.preventDefault();$('#props').classList.toggle('hidden')};zoom($('#single'));for(let op of ['save','submit','discard'])if($('#'+op))$('#'+op).onclick=()=>action(async()=>{await api(op,payload(p,$('#meta')));await refresh();openPhoto(id);toast(op==='submit'?'已正式提交':'已儲存')});$('#star').onclick=()=>action(async()=>{await api('star',{photo:id});await refresh();openPhoto(id)});$('#addtag').onclick=()=>openTagDialog({title:'新增照片標籤',onConfirm:async tags=>{await api('tags',{photo:id,tags});await refresh();openPhoto(id)}});if($('#anomaly'))$('#anomaly').onclick=()=>openAnomalyDialog(id,()=>{openPhoto(id)});if($('#move'))$('#move').onclick=()=>openMoveDialog(p);if($('#restore'))$('#restore').onclick=()=>action(async()=>{await api('restore',{photo:id});await refresh();openPhoto(id)});document.querySelectorAll('[data-resolve]').forEach(b=>b.onclick=()=>{const a=p.anomalies.find(x=>x.id===+b.dataset.resolve);if(a)openResolveAnomalyDialog(a,async()=>{await refresh();openPhoto(id)})});$('#compare').onclick=()=>compareView(p);if($('#pick-photo-location'))$('#pick-photo-location').onclick=()=>{const form=$('#meta');openMapPickerModal({lat:form.lat.value?Number(form.lat.value):null,lon:form.lon.value?Number(form.lon.value):null,datasetId:p.dataset,onPick:(la,lo)=>{form.lat.value=la.toFixed(6);form.lon.value=lo.toFixed(6);toast('已套用新座標，請記得儲存或提出更正申請')}})};if(typeof enhancePhotoModal==='function')enhancePhotoModal(p)}
function openTagDialog({title,hint,onConfirm}){
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const known=availableTags();
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="toolbar"><h2>${esc(title)}</h2><button type="button" data-ux-close>關閉</button></div>${hint?`<p class="muted">${esc(hint)}</p>`:''}<form id="ux-tag-form"><label for="ux-tag-input">標籤名稱（多個請以逗號分隔）</label><input id="ux-tag-input" required placeholder="例如：護岸巡查, 待修繕" autocomplete="off">${known.length?`<div class="ux-quick-section"><span class="muted">點擊加入現有常用標籤：</span><div class="tag-chips">${known.map(t=>`<button type="button" class="tag-chip" data-quick-tag="${esc(t)}">＋ ${esc(t)}</button>`).join('')}</div></div>`:''}<div class="row" style="margin-top:16px"><button type="button" data-ux-close>取消</button><button type="submit" class="primary">確認新增</button></div></form></section>`;
 document.body.append(m);
 const input=$('#ux-tag-input');input.focus();
 m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
 m.querySelectorAll('[data-quick-tag]').forEach(b=>b.onclick=()=>{const cur=input.value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);if(!cur.includes(b.dataset.quickTag))cur.push(b.dataset.quickTag);input.value=cur.join(', ');input.focus()});
 $('#ux-tag-form').onsubmit=e=>{e.preventDefault();const tags=input.value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);if(!tags.length)return toast('請輸入至少一個標籤');action(async()=>{await onConfirm(tags);m.remove()})};
}
function openAnomalyDialog(photoId,onDone){
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const presets=['護岸／堤防損壞','河道淤積／阻塞','設施／水門故障','水尺／標誌不清','拍攝時間／位置需核對'];
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="人工異常標註"><div class="toolbar"><h2>人工異常標註</h2><button type="button" data-ux-close>關閉</button></div><form id="ux-anomaly-form"><div class="ux-quick-section"><span class="muted">常用異常類型（點擊帶入）：</span><div class="tag-chips">${presets.map(t=>`<button type="button" class="tag-chip" data-preset="${esc(t)}">${esc(t)}</button>`).join('')}</div></div><label for="ux-anomaly-text">異常類型與現場說明（必填）</label><textarea id="ux-anomaly-text" required placeholder="請說明發現的異常狀況與位置"></textarea><div class="row" style="margin-top:16px"><button type="button" data-ux-close>取消</button><button type="submit" class="primary">送出異常標註</button></div></form></section>`;
 document.body.append(m);
 const text=$('#ux-anomaly-text');text.focus();
 m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
 m.querySelectorAll('[data-preset]').forEach(b=>b.onclick=()=>{const p=b.dataset.preset;if(!text.value.includes(p))text.value=text.value?`【${p}】${text.value}`:`【${p}】`;text.focus()});
 $('#ux-anomaly-form').onsubmit=e=>{e.preventDefault();const v=text.value.trim();if(!v)return toast('請填異常類型與說明');action(async()=>{await api('anomaly',{photo:photoId,text:v});m.remove();await refresh();if(onDone)onDone();toast('已新增人工異常標註')})};
}
function openMoveDialog(p){
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const targets=state.datasets.filter(d=>d.item_id==p.item&&!d.archived&&d.id!==p.dataset);
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="申請搬移資料集"><div class="toolbar"><h2>申請搬移資料集</h2><button type="button" data-ux-close>關閉</button></div><p class="muted">目前資料集：${esc(ds(p.dataset)?.name)}</p>${targets.length?`<form id="ux-move-form"><label for="ux-move-target">目的資料集（限同表單範本且未封存）</label><select id="ux-move-target" required>${targets.map(d=>`<option value="${d.id}">${esc(d.name)}</option>`).join('')}</select><label for="ux-move-reason">申請搬移理由（必填）</label><textarea id="ux-move-reason" required placeholder="請說明需搬移至該資料集的原因"></textarea><div class="row" style="margin-top:16px"><button type="button" data-ux-close>取消</button><button type="submit" class="primary">送出搬移申請</button></div></form>`:'<p class="metadata-warning">目前沒有其他可搬移的同表單未封存資料集。</p><button type="button" data-ux-close>關閉</button>'}</section>`;
 document.body.append(m);
 m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
 if($('#ux-move-form'))$('#ux-move-form').onsubmit=e=>{e.preventDefault();const reason=$('#ux-move-reason').value.trim();if(!reason)return toast('請填申請理由');action(async()=>{await api('request',{photo:p.id,version:p.version,kind:'move',reason,payload:{dataset:+$('#ux-move-target').value}});m.remove();await refresh();toast('搬移申請已送出，可到案件與通知查看')})};
}
function openResolveAnomalyDialog(a,onDone){
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="處理異常紀錄"><div class="toolbar"><h2>處理人工異常</h2><button type="button" data-ux-close>關閉</button></div><p class="request-reason">${esc(a.text)}</p><form id="ux-resolve-form"><label>判定狀態</label><div class="ux-radio-group">${[['confirmed','確認異常'],['dismissed','排除（無異常）'],['pending','重開為待確認']].map(([v,l])=>`<label class="check"><input type="radio" name="state" value="${v}" ${(a.state==='pending'&&v==='confirmed')||a.state===v?'checked':''}>${l}</label>`).join('')}</div><label for="ux-resolve-reason">處理說明（必填）</label><textarea id="ux-resolve-reason" required placeholder="請填寫查核結果或處理方式">${esc(a.resolution||'')}</textarea><div class="row" style="margin-top:16px"><button type="button" data-ux-close>取消</button><button type="submit" class="primary">儲存處理結果</button></div></form></section>`;
 document.body.append(m);
 m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=()=>m.remove());
 $('#ux-resolve-form').onsubmit=e=>{e.preventDefault();const fd=new FormData(e.target),reason=($('#ux-resolve-reason').value||'').trim();if(!reason)return toast('請填寫處理說明');action(async()=>{await api('resolve',{id:a.id,state:fd.get('state'),reason});m.remove();if(onDone)await onDone();toast('異常狀態已更新')})};
}
async function openMapPickerModal({lat,lon,datasetId,onPick}){
 if(!window.L)return toast('地圖元件尚未就緒');
 document.querySelector('.ux-dialog-backdrop')?.remove();
 let refPhoto=(state.photos||[]).find(x=>x.dataset===datasetId&&x.lat!=null&&x.lon!=null);
 if(!refPhoto&&lat==null&&datasetId){try{const res=await api('search',{datasets:[datasetId],limit:20});refPhoto=(res.photos||[]).find(x=>x.lat!=null&&x.lon!=null)}catch{}}
 const initLat=lat??refPhoto?.lat??25.033,initLon=lon??refPhoto?.lon??121.565,initZoom=(lat!=null||refPhoto)?15:11;
 let chosen=lat!=null&&lon!=null?[lat,lon]:null;
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 m.innerHTML=`<section class="card ux-dialog ux-map-dialog" role="dialog" aria-modal="true" aria-label="地圖點選定位"><div class="toolbar"><h2>地圖點選定位</h2><button type="button" data-ux-close>關閉</button></div><p class="muted">點擊地圖任意位置設定拍攝座標（支援縮放平移與電子地圖／航照圖切換）。</p><div id="ux-picker-map"></div><p id="ux-picker-coords" class="muted">${chosen?`已選座標：${chosen[0].toFixed(6)}, ${chosen[1].toFixed(6)}`:'尚未點選位置'}</p><div class="row"><button type="button" data-ux-close>取消</button><button type="button" id="ux-picker-confirm" class="primary" ${chosen?'':'disabled'}>確認套用座標</button></div></section>`;
 document.body.append(m);
 const pickerMap=L.map('ux-picker-map').setView([initLat,initLon],initZoom);
 attachBaseMaps(pickerMap);
 let marker=chosen?L.circleMarker(chosen,{radius:8,color:'#135c58',fillColor:'#218d7b',fillOpacity:.9}).addTo(pickerMap):null;
 pickerMap.on('click',e=>{chosen=[e.latlng.lat,e.latlng.lng];if(marker)marker.setLatLng(chosen);else marker=L.circleMarker(chosen,{radius:8,color:'#135c58',fillColor:'#218d7b',fillOpacity:.9}).addTo(pickerMap);$('#ux-picker-coords').textContent=`已選座標：${chosen[0].toFixed(6)}, ${chosen[1].toFixed(6)}`;$('#ux-picker-confirm').disabled=false});
 setTimeout(()=>pickerMap.invalidateSize(),60);
 const close=()=>{pickerMap.remove();m.remove()};
 m._uxCleanup=close;
 m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=close);
 $('#ux-picker-confirm').onclick=()=>{if(chosen){onPick(chosen[0],chosen[1]);close()}};
}
function zoom(area){let scale=1,x=0,y=0,last=null;function paint(){area.querySelector('img').style.transform=`translate(${x}px,${y}px) scale(${scale})`}area.onwheel=e=>{e.preventDefault();scale=Math.max(.2,Math.min(12,scale*(e.deltaY>0?.9:1.1)));paint()};area.onpointerdown=e=>{last=[e.clientX,e.clientY];area.setPointerCapture(e.pointerId)};area.onpointermove=e=>{if(last){x+=e.clientX-last[0];y+=e.clientY-last[1];last=[e.clientX,e.clientY];paint()}};area.onpointerup=()=>last=null}
function distance(a,b){let rad=x=>x*Math.PI/180,dlat=rad(b.lat-a.lat),dlon=rad(b.lon-a.lon);return 6371008.8*2*Math.asin(Math.sqrt(Math.sin(dlat/2)**2+Math.cos(rad(a.lat))*Math.cos(rad(b.lat))*Math.sin(dlon/2)**2))}
function compareView(p){openComparison(p)}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){const top=[...document.querySelectorAll('.ux-dialog-backdrop,.request-dialog-backdrop,.form-preview-backdrop')].pop();if(top){if(top._uxCleanup)top._uxCleanup();else top.remove();return}document.querySelector('.modal')?.remove()}if(e.code==='Space'&&document.querySelector('.modal')&&!document.querySelector('.ux-dialog-backdrop,.request-dialog-backdrop')&&!['INPUT','TEXTAREA','SELECT','BUTTON'].includes(document.activeElement.tagName)){e.preventDefault();$('#props')?.classList.toggle('hidden')}});
function inbox(){renderWorkflow()}

function admin(){renderAdmin()}

action(async()=>{try{await refresh()}catch(e){loginView()}});
let lastTagBatch=null;
