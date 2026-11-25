const uploadWorkspace={department:null,dataset:null,active:null,selected:new Set(),edits:new Map(),messages:[],busy:false,rows:[],loadedDataset:null,loading:false};
function resetUpload(){uploadWorkspace.department=null;uploadWorkspace.dataset=null;uploadWorkspace.active=null;uploadWorkspace.selected.clear();uploadWorkspace.edits.clear();uploadWorkspace.messages=[];uploadWorkspace.rows=[];uploadWorkspace.loadedDataset=null;uploadWorkspace.loading=false}
function uploadPhoto(p){return uploadWorkspace.edits.get(p.id)||{...p,attrs:structuredClone(p.attrs)}}
function uploadRows(){return (uploadWorkspace.rows||[]).filter(p=>p.dataset===uploadWorkspace.dataset&&p.status==='draft'&&(p.uploader===state.user.name||p.can_manage))}
async function loadUploadDrafts(dsId){
 const w=uploadWorkspace;if(!dsId){w.rows=[];w.loadedDataset=null;return}
 w.loading=true;
 try{
  const r=await api('search',{datasets:[dsId],status:'draft',limit:200});
  if(w.dataset===dsId){w.rows=r.photos||[];w.loadedDataset=dsId;if(typeof photoCacheUpsert==='function')photoCacheUpsert(w.rows)}
 }finally{w.loading=false}
}
function uploadChosen(){return uploadRows().filter(p=>uploadWorkspace.selected.has(p.id))}
function localCapture(value){return value?new Date(new Date(value).getTime()+8*3600000).toISOString().slice(0,19):''}
function displayCapture(value){return value?new Date(value).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'}):'未擷取到拍攝時間'}
function missingFields(p){return p.fields.filter(f=>f.required&&(p.attrs[f.key]==null||p.attrs[f.key]===''||(Array.isArray(p.attrs[f.key])&&!p.attrs[f.key].length))).map(f=>f.label)}
function uploadControl(f,value,prefix){
 const id=prefix+'-'+f.key,name='attr_'+f.key,base=`id="${esc(id)}" name="${esc(name)}"`;
 let control;
 if(['select','multi'].includes(f.type))control=`<select ${base} ${f.type==='multi'?'multiple':''}>${f.type==='select'?'<option value="">請選擇</option>':''}${(f.options||[]).map(v=>`<option value="${esc(v)}" ${(Array.isArray(value)?value.includes(v):value===v)?'selected':''}>${esc(v)}</option>`).join('')}</select>`;
 else if(f.type==='boolean')control=`<select ${base}><option value="">未填</option><option value="true" ${value===true?'selected':''}>是</option><option value="false" ${value===false?'selected':''}>否</option></select>`;
 else if(f.type==='textarea')control=`<textarea ${base}>${esc(value??'')}</textarea>`;
 else control=`<input ${base} type="${['integer','number'].includes(f.type)?'number':f.type==='date'?'date':'text'}" step="${f.type==='integer'?'1':'any'}" value="${esc(value??'')}">`;
 return `<label for="${esc(id)}">${esc(f.label)}${f.required?' <span class="error">*</span>':''}</label>${control}`;
}
function renderUpload(){
 const w=uploadWorkspace,departments=state.departments.filter(d=>state.user.admin||(state.user.writable||[]).includes(d.id));
 if(!departments.some(d=>d.id===w.department))w.department=departments[0]?.id||null;
 const datasets=state.datasets.filter(d=>item(d.item_id)?.department_id===w.department&&!d.archived&&item(d.item_id)?.active);
 if(!datasets.some(d=>d.id===w.dataset)){w.dataset=datasets[0]?.id||null;w.active=null;w.selected.clear()}
 const dataset=ds(w.dataset),template=dataset?item(dataset.item_id):null,rows=uploadRows();
 if(!rows.some(p=>p.id===w.active))w.active=rows[0]?.id||null;
 $('#side').innerHTML=`<h3>上傳與提交</h3><ol class="upload-steps"><li>選擇科室與資料集</li><li>檢查照片與填寫表單</li><li>確認後正式提交</li></ol><p class="muted">照片先保存在草稿，確認提交前不會出現在一般圖台。</p><p class="muted">缺少拍攝時間或位置仍可提交為待補資料；自訂必填欄位須完整。</p>`;
 $('#main').innerHTML=`<div class="toolbar"><div><h1>上傳照片</h1><p class="muted">批次填共同資料，再逐張檢查現場紀錄。</p></div></div>
 <section class="card"><div class="upload-destination"><div><label for="upload-department">科室</label><select id="upload-department" ${w.busy?'disabled':''}>${option(departments,w.department)}</select></div><div><label for="upload-dataset">資料集</label><select id="upload-dataset" ${w.busy?'disabled':''}>${datasets.length?option(datasets,w.dataset):'<option value="">無可上傳的資料集</option>'}</select></div><div class="assigned-form"><span class="muted">自動帶入指定表單</span><strong>${esc(template?.name||'尚未指定')}</strong>${template?`<span class="badge">v${template.version} · ${template.fields.length} 個欄位</span>`:''}</div></div>
 ${template?`<details class="assigned-preview"><summary>查看本次使用的表單欄位</summary><div class="row">${template.fields.map(f=>`<span class="badge">${esc(f.label)}${f.required?' · 必填':''}</span>`).join('')||'<span class="muted">僅系統時間與座標欄位</span>'}</div></details>`:''}
 <form id="upload-files-form"><div class="upload-zone"><label for="upload-files">加入照片以擷取時間、座標與建立預覽</label><input id="upload-files" type="file" multiple accept="image/jpeg,image/png,image/webp,image/heic" ${!template||w.busy?'disabled':''}><p class="muted">每批最多 500 張，每張 50 MB。本機可讀 JPEG、PNG、WebP；HEIC 需另裝解碼器。</p><button class="primary" id="receive-files" ${!template||w.busy?'disabled':''}>${w.busy?'處理中…':'上傳並建立草稿'}</button></div></form><div id="upload-progress" role="status"></div>
 ${w.messages.length?`<details class="upload-results" open><summary>最近操作結果</summary>${w.messages.map(m=>`<p class="${m.error?'error':''}">${esc(m.name)}：${esc(m.message)}</p>`).join('')}</details>`:''}</section>
 <div class="toolbar"><h2>檢查草稿 <span class="muted">${rows.length} 張</span></h2><button id="upload-select-all" ${w.busy?'disabled':''}>全選草稿</button><button id="upload-select-none" ${w.busy?'disabled':''}>取消全選</button><span id="upload-selection-count"></span></div>
 <div id="upload-bulk"></div><div class="upload-review-workspace"><section id="upload-photo-list" class="upload-photo-list"></section><section id="upload-photo-editor" class="card upload-photo-editor"></section></div>
 <div class="upload-submit-bar"><span id="upload-save-status" role="status">草稿可先儲存，稍後繼續。</span><button id="save-upload-selected" ${w.busy?'disabled':''}>儲存已選草稿</button><button id="review-upload-submit" class="primary" ${w.busy?'disabled':''}>檢查並確認提交</button></div>`;
 $('#upload-department').onchange=e=>{w.department=+e.target.value;w.dataset=null;w.loadedDataset=null;w.rows=[];w.selected.clear();renderUpload()};$('#upload-dataset').onchange=e=>{w.dataset=+e.target.value;w.loadedDataset=null;w.rows=[];w.active=null;w.selected.clear();w.messages=[];renderUpload()};
 $('#upload-files-form').onsubmit=e=>{e.preventDefault();action(receiveUploadFiles)};
 $('#upload-select-all').onclick=()=>{rows.forEach(p=>w.selected.add(p.id));drawUploadList();drawUploadBulk()};$('#upload-select-none').onclick=()=>{w.selected.clear();drawUploadList();drawUploadBulk()};
 $('#save-upload-selected').onclick=()=>action(async()=>{const chosen=uploadChosen();if(!chosen.length)return toast('請先勾選草稿');await persistUploadRows(chosen);renderUpload()});
 $('#review-upload-submit').onclick=()=>action(reviewUploadSubmit);
 drawUploadList();drawUploadBulk();drawUploadEditor();
 if(w.dataset&&w.loadedDataset!==w.dataset&&!w.loading){action(async()=>{await loadUploadDrafts(w.dataset);if(tab==='upload')renderUpload()})}
}
function drawUploadList(){
 const w=uploadWorkspace,rows=uploadRows();$('#upload-selection-count').textContent=`已選 ${uploadChosen().length} 張`;
 $('#upload-photo-list').innerHTML=rows.length?rows.map(raw=>{const p=uploadPhoto(raw),missing=missingFields(p);return `<article class="draft-tile ${w.active===p.id?'is-current':''}"><label class="draft-select"><input type="checkbox" data-upload-check="${p.id}" ${w.selected.has(p.id)?'checked':''} ${w.busy?'disabled':''} aria-label="選取 ${esc(p.name)}"></label><button type="button" data-upload-open="${p.id}" ${w.busy?'disabled':''}><img loading="lazy" src="${p.preview}" alt="${esc(p.name)}"><strong>${esc(p.name)}</strong><span class="muted">${esc(displayCapture(p.captured))}</span><span class="draft-coordinates">${p.lat==null?'待補座標':`${Number(p.lat).toFixed(6)}, ${Number(p.lon).toFixed(6)}`}</span><span class="badge ${missing.length?'warn':''}">${missing.length?'缺 '+missing.length+' 個必填':'表單已填妥'}</span>${w.edits.has(p.id)?'<span class="badge warn">未儲存修改</span>':''}</button></article>`}).join(''):'<div class="empty">選擇照片上傳後，會在這裡顯示草稿與擷取結果。</div>';
 document.querySelectorAll('[data-upload-check]').forEach(x=>x.onchange=()=>{x.checked?w.selected.add(+x.dataset.uploadCheck):w.selected.delete(+x.dataset.uploadCheck);$('#upload-selection-count').textContent=`已選 ${uploadChosen().length} 張`;drawUploadBulk()});
 document.querySelectorAll('[data-upload-open]').forEach(b=>b.onclick=()=>{w.active=+b.dataset.uploadOpen;drawUploadList();drawUploadEditor()});
}
function drawUploadBulk(){
 const w=uploadWorkspace,chosen=uploadChosen().map(uploadPhoto);
 if(!chosen.length){$('#upload-bulk').innerHTML='<p class="muted">勾選照片後，可批次套用共同表單欄位。未勾選的照片不會被改動。</p>';return}
 const shared=chosen[0].fields.filter(f=>chosen.every(p=>p.fields.some(x=>x.key===f.key&&x.type===f.type&&JSON.stringify(x.options||[])===JSON.stringify(f.options||[]))));
 $('#upload-bulk').innerHTML=`<details class="card bulk-panel" open><summary>批次填寫共同欄位 · 已選 ${chosen.length} 張</summary><p class="muted">只套用勾選的欄位，會覆蓋這些照片的對應值；其他欄位與每張照片的 EXIF 時間、座標保持原值。</p><form id="bulk-upload-form"><fieldset ${w.busy?'disabled':''}>${shared.map(f=>`<div class="bulk-field"><label class="check"><input type="checkbox" name="apply" value="${esc(f.key)}" aria-label="套用 ${esc(f.label)}">套用此欄位</label>${uploadControl(f,'','bulk')}</div>`).join('')||'<p class="muted">所選照片沒有相容的共同欄位。請逐張修改，或按相同表單版本分批選取。</p>'}<button id="apply-upload-common" type="submit" ${shared.length?'':'disabled'}>套用至已選 ${chosen.length} 張</button></fieldset></form></details>`;
 $('#bulk-upload-form').onsubmit=e=>{e.preventDefault();const fd=new FormData(e.target),keys=fd.getAll('apply');if(!keys.length)return toast('請勾選要套用的欄位');const values=payload({id:0,version:0,fields:shared},e.target).attrs;for(const raw of uploadChosen()){const p=uploadPhoto(raw);p.attrs={...p.attrs};for(const key of keys)p.attrs[key]=structuredClone(values[key]);w.edits.set(p.id,p)}drawUploadList();drawUploadEditor();toast(`已套用 ${keys.length} 個欄位至 ${chosen.length} 張草稿，確認後請儲存或提交`)};
}
function drawUploadEditor(){
 const w=uploadWorkspace,rows=uploadRows(),idx=rows.findIndex(p=>p.id===w.active),raw=idx>=0?rows[idx]:null;
 if(!raw){$('#upload-photo-editor').innerHTML='<div class="empty">點選左側照片逐張檢查與修改。</div>';return}
 const p=uploadPhoto(raw),original=p.extracted||{},hasOriginal=Object.keys(original).length>0;
 const prevLocated=rows.slice(0,idx).reverse().map(uploadPhoto).find(x=>x.lat!=null&&x.lon!=null)||rows.map(uploadPhoto).find(x=>x.id!==p.id&&x.lat!=null&&x.lon!=null);
 $('#upload-photo-editor').innerHTML=`<div class="toolbar"><h3>${esc(p.name)}</h3><span class="badge">表單 v${p.form_version}</span><span class="muted">${idx+1} / ${rows.length}</span><button type="button" id="upload-prev-draft" ${idx<=0||w.busy?'disabled':''} title="Alt + ←">← 上一張</button><button type="button" id="upload-next-draft" ${idx>=rows.length-1||w.busy?'disabled':''} title="Alt + →">下一張 →</button></div><img class="draft-large-preview" src="${p.preview}" alt="${esc(p.name)}">
 <details class="exif-summary" open><summary>照片自動擷取資訊（原始值）</summary><dl><dt>拍攝時間</dt><dd>${esc(displayCapture(original.captured))}</dd><dt>時區</dt><dd>${original.raw_offset?esc(original.raw_offset):original.captured?'照片未提供，暫以臺灣時間解讀':'未提供'}</dd><dt>GPS 座標</dt><dd>${original.lat==null?'未擷取到有效 GPS':`${esc(original.lat)}, ${esc(original.lon)}`}</dd></dl>${!hasOriginal?'<p class="muted">舊版草稿未保存擷取來源，請依目前資料人工確認。</p>':(original.warnings||[]).map(m=>`<p class="metadata-warning">${esc(m)}</p>`).join('')}</details>
 <form id="individual-upload-form"><fieldset ${w.busy?'disabled':''}><h3>確認／修正拍攝資訊</h3><label for="draft-captured">拍攝時間（臺灣時間）</label><input id="draft-captured" name="captured" type="datetime-local" step="1" value="${localCapture(p.captured)}"><label class="check"><input type="checkbox" name="time_confirmed" ${p.time_confirmed?'checked':''}>已確認拍攝時間與時區</label><div class="row"><div><label for="draft-lat">緯度</label><input id="draft-lat" name="lat" type="number" step="any" min="-90" max="90" value="${p.lat??''}"></div><div><label for="draft-lon">經度</label><input id="draft-lon" name="lon" type="number" step="any" min="-180" max="180" value="${p.lon??''}"></div></div><div class="row" style="margin-top:8px"><button type="button" id="draft-pick-map">地圖點選定位</button>${prevLocated?`<button type="button" id="draft-copy-loc">套用他張座標（${esc(prevLocated.name)}）</button>`:''}</div>${p.county?`<p class="muted" style="margin:6px 0">行政區（自動反查）：<strong>${esc(p.county)} ${esc(p.town)} ${esc(p.village)}</strong></p>`:''}<p class="muted">留空可列為待補資料；手動修正不會覆寫上方原始擷取值。</p><h3>照片屬性表單</h3>${p.fields.map(f=>uploadControl(f,p.attrs[f.key],'single')).join('')||'<p class="muted">此照片僅使用系統欄位。</p>'}<div class="row" style="margin-top:14px"><button type="button" id="save-one-draft">儲存這張草稿</button><button type="button" class="primary" id="save-and-next-draft" ${rows.length<=1?'disabled':''}>儲存並跳至下一張待補</button></div></fieldset></form>`;
 const form=$('#individual-upload-form');function changed(){const next=payload(p,form);next.lat=next.lat===''?null:Number(next.lat);next.lon=next.lon===''?null:Number(next.lon);w.edits.set(p.id,{...p,...next});drawUploadList();$('#upload-save-status').textContent='有尚未儲存的修改，提交前會先儲存。'}form.oninput=form.onchange=changed;
 $('#upload-prev-draft').onclick=()=>{if(idx>0){w.active=rows[idx-1].id;drawUploadList();drawUploadEditor()}};
 $('#upload-next-draft').onclick=()=>{if(idx<rows.length-1){w.active=rows[idx+1].id;drawUploadList();drawUploadEditor()}};
 $('#draft-pick-map').onclick=()=>openMapPickerModal({lat:p.lat,lon:p.lon,datasetId:p.dataset,onPick:(la,lo)=>{form.lat.value=la.toFixed(6);form.lon.value=lo.toFixed(6);changed();toast('已套用座標至草稿')}});
 if($('#draft-copy-loc'))$('#draft-copy-loc').onclick=()=>{form.lat.value=Number(prevLocated.lat).toFixed(6);form.lon.value=Number(prevLocated.lon).toFixed(6);changed();toast(`已套用 ${prevLocated.name} 的座標`)};
 $('#save-one-draft').onclick=()=>action(async()=>{await persistUploadRows([raw]);renderUpload()});
 $('#save-and-next-draft').onclick=()=>action(async()=>{await persistUploadRows([raw]);if(!w.messages.some(m=>m.error)){const freshRows=uploadRows();const nextIncomplete=freshRows.find(x=>x.id!==raw.id&&(missingFields(x).length||x.lat==null))||freshRows[idx+1]||freshRows[0];if(nextIncomplete)w.active=nextIncomplete.id}renderUpload()});
}
async function receiveUploadFiles(){
 const w=uploadWorkspace,files=[...$('#upload-files').files],dataset=ds(w.dataset),template=dataset?item(dataset.item_id):null;
 if(!files.length)return toast('請先選擇照片');if(files.length>500)return toast('每批最多 500 張');if(!template)return toast('請先選擇科室與資料集');
 w.busy=true;w.messages=[];renderUpload();let ids=[];
 try{
  for(let i=0;i<files.length;i++){
   const file=files[i];$('#upload-progress').textContent=`正在接收 ${i+1} / ${files.length}：${file.name}`;
   if(file.size>50*1024*1024){w.messages.push({name:file.name,error:true,message:'超過 50 MB'});continue}
   const fd=new FormData();fd.append('dataset',dataset.id);fd.append('form_id',template.id);fd.append('form_version',template.version);fd.append('files',file);
   try{const r=await api('upload',fd);for(const result of r.results){w.messages.push({name:result.name,error:!!result.error,message:result.error||'已建立草稿，請檢查後提交'});if(result.id)ids.push(result.id)}}catch(e){w.messages.push({name:file.name,error:true,message:e.message});if(e.message.includes('表單已更新'))break}
  }
  state=await api('state');await loadUploadDrafts(w.dataset);w.selected=new Set(ids);w.active=ids[0]||w.active;
 }finally{w.busy=false;renderUpload()}
}
async function persistUploadRows(rows){
 const w=uploadWorkspace;w.busy=true;w.messages=[];renderUpload();
 try{
  const items=rows.map(raw=>{const p=uploadPhoto(raw);return {photo:p.id,version:p.version,captured:p.captured,time_confirmed:p.time_confirmed,lat:p.lat,lon:p.lon,attrs:p.attrs}});
  if(items.length){
   const r=await api('batch_save',{items});
   for(const res of r.results){
    if(res.error){w.messages.push({name:res.name,error:true,message:res.error})}
    else{w.edits.delete(res.id);w.messages.push({name:res.name,message:'草稿已儲存'})}
   }
   state=await api('state');await loadUploadDrafts(w.dataset);
  }
 }catch(e){w.messages.push({name:'批次儲存',error:true,message:e.message})}
 finally{w.busy=false}
}
async function reviewUploadSubmit(){
 const w=uploadWorkspace,chosen=uploadChosen();if(!chosen.length)return toast('請先勾選要提交的照片');
 await persistUploadRows(chosen);renderUpload();
 const failed=new Set(w.messages.filter(m=>m.error).map(m=>m.name));
 const rows=uploadChosen().filter(p=>!failed.has(p.name)&&!w.edits.has(p.id)),valid=rows.filter(p=>!missingFields(p).length),invalid=rows.filter(p=>missingFields(p).length);
 const overlay=document.createElement('div');overlay.className='form-preview-backdrop';overlay.innerHTML=`<section class="card submit-review" role="dialog" aria-modal="true" aria-label="確認提交照片"><h2>確認提交照片</h2><p>本次勾選 ${chosen.length} 張，可提交 ${valid.length} 張。</p>${invalid.length?`<div class="metadata-warning">以下 ${invalid.length} 張保留草稿，請補填必填欄位：${invalid.map(p=>`<p>${esc(p.name)}：${esc(missingFields(p).join('、'))}</p>`).join('')}</div>`:''}${w.messages.filter(m=>m.error).map(m=>`<p class="error">${esc(m.name)}：${esc(m.message)}</p>`).join('')}<div class="submit-review-list">${valid.map(p=>`<div><strong>${esc(p.name)}</strong><p>${esc(displayCapture(p.captured))} · ${p.lat==null?'待補座標':`${esc(p.lat)}, ${esc(p.lon)}`}</p><small>${p.fields.map(f=>esc(f.label)+': '+esc(Array.isArray(p.attrs[f.key])?p.attrs[f.key].join('、'):p.attrs[f.key]??'未填')).join(' · ')}</small></div>`).join('')}</div><p class="muted">${valid.filter(p=>!p.captured||p.lat==null).length} 張缺少時間或位置，提交後會列為待補。提交後修改正式資料需提出更正申請。</p><label class="check"><input id="confirm-upload-reviewed" type="checkbox">我已檢查表單、拍攝時間與座標，確認提交以上可提交照片</label><div class="row"><button id="cancel-upload-review">返回修改</button><button id="confirm-upload-submit" class="primary" disabled>確認提交 ${valid.length} 張</button></div></section>`;document.body.append(overlay);
 $('#confirm-upload-reviewed').onchange=e=>$('#confirm-upload-submit').disabled=!e.target.checked||!valid.length;$('#cancel-upload-review').onclick=()=>overlay.remove();
 $('#confirm-upload-submit').onclick=()=>action(async()=>{const button=$('#confirm-upload-submit');button.disabled=true;$('#cancel-upload-review').disabled=true;w.busy=true;w.messages=[];try{const items=valid.map(p=>({photo:p.id,version:p.version,captured:p.captured,time_confirmed:p.time_confirmed,lat:p.lat,lon:p.lon,attrs:p.attrs}));const r=await api('batch_submit',{items});for(const res of r.results){if(res.error){w.messages.push({name:res.name,error:true,message:res.error})}else{w.messages.push({name:res.name,message:'已正式提交'});w.selected.delete(res.id)}}state=await api('state');await loadUploadDrafts(w.dataset);toast(`已提交 ${w.messages.filter(m=>!m.error).length} 張；未成功者保留草稿`)}finally{w.busy=false;overlay.remove();renderUpload()}});
}

window.addEventListener('beforeunload',e=>{if(uploadWorkspace.edits.size||uploadWorkspace.busy){e.preventDefault();e.returnValue=''}});
document.addEventListener('keydown',e=>{if(tab==='upload'&&e.altKey&&(e.key==='ArrowLeft'||e.key==='ArrowRight')){const btn=$(e.key==='ArrowLeft'?'#upload-prev-draft':'#upload-next-draft');if(btn&&!btn.disabled){e.preventDefault();btn.click()}}});
