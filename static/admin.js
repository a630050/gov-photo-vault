let adminSection='forms',editor=null,formDirty=false;
const fieldTypes=[['text','單行文字'],['textarea','多行文字'],['integer','整數'],['number','小數'],['date','日期'],['select','下拉單選'],['multi','多選選項'],['boolean','是／否']];
function managedDepartments(){return state.departments.filter(d=>state.user.admin||state.user.managed.includes(d.id)||state.user.capabilities?.[d.id]?.forms)}
function renderAdmin(){
 const departments=managedDepartments();
 $('#side').innerHTML=`<h3>管理後台</h3><div class="admin-nav">${[['forms','表單設計'],['datasets','資料集管理'],['accounts','帳號與設定'],['integrations','串接系統管理'],['operations','部署與維運']].filter(([id])=>!['accounts','integrations','operations'].includes(id)||state.user.admin).map(([id,label])=>`<button data-admin-section="${id}" class="${adminSection===id?'primary':''}">${label}</button>`).join('')}</div><p class="muted">科室 → 資料集 → 照片<br>表單是資料集使用的欄位範本。</p>`;
 document.querySelectorAll('[data-admin-section]').forEach(b=>b.onclick=()=>{if(formDirty&&!confirm('尚有未發布的表單修改，確定離開？'))return;formDirty=false;editor=null;adminSection=b.dataset.adminSection;renderAdmin()});
 if(!departments.length){$('#main').innerHTML='<div class="empty">目前沒有科室表單管理權限，請由主管或系統管理者操作。</div>';return}
 if(adminSection==='operations'){renderOperations();return}
 if(adminSection==='integrations'){renderIntegrations();return}
 if(adminSection==='accounts'){renderAccounts();return}
 if(adminSection==='datasets'){renderDatasets(departments);return}
 if(!editor)editor={id:null,department:departments[0].id,name:'',version:null,active:true,fields:[]};
 renderFormEditor(departments);
}
function editorFrom(template){return {id:template.id,department:template.department_id,name:template.name,version:template.version,active:template.active,fields:structuredClone(template.fields)}}
function renderFormEditor(departments){
 $('#main').innerHTML=`<div class="toolbar"><div><h1>表單設計</h1><p class="muted">用欄位按鈕建立表單，發布後可指定給同科室資料集。</p></div></div>
 <div class="form-workspace"><section class="card template-list"><div class="toolbar"><h3>表單範本</h3><button id="new-template">＋ 新表單</button></div>${departments.map(d=>`<h4>${esc(d.name)}</h4>${state.items.filter(i=>i.department_id===d.id).map(i=>`<button class="template-choice ${editor.id===i.id?'is-current':''}" data-template="${i.id}"><strong>${esc(i.name)}</strong><span>v${i.version} · ${i.fields.length} 個欄位 · ${i.active?'使用中':'已停用'}</span></button>`).join('')||'<p class="muted">尚無表單範本</p>'}`).join('')}</section>
 <section class="card form-editor"><div class="toolbar"><h2>${editor.id?'編輯表單':'建立表單'}</h2><span class="badge">${editor.id?'目前 v'+editor.version:'尚未發布'}</span></div>
 <div class="row"><div><label for="form-department">所屬科室</label><select id="form-department" ${editor.id?'disabled':''}>${option(departments,editor.department)}</select></div><div><label for="form-name">表單名稱</label><input id="form-name" maxlength="100" value="${esc(editor.name)}" placeholder="例如 道路現場紀錄"></div></div>
 <label class="check"><input type="checkbox" id="form-active" ${editor.active?'checked':''}>允許新資料集指定及新照片上傳</label>
 <div class="system-fields"><strong>系統欄位</strong><p>拍攝時間、經緯度、上傳者由系統提供，不需另建欄位。</p></div>
 <div id="field-editor"></div><div class="add-field-types"><span>新增欄位</span>${fieldTypes.map(([type,label])=>`<button type="button" data-add-field="${type}">＋ ${label}</button>`).join('')}</div>
 <div class="publish-bar"><span id="form-message" class="muted" role="status"></span><button id="preview-form">預覽表單</button><button class="primary" id="publish-form">${editor.id?'發布新版本':'發布表單'}</button></div><p class="muted">發布只影響之後上傳的照片；既有照片與草稿保留原欄位及資料。重新指定資料集也不會覆寫歷史照片。</p></section></div>`;
 $('#form-name').oninput=e=>{editor.name=e.target.value;formDirty=true};$('#form-department').onchange=e=>{editor.department=+e.target.value;formDirty=true};$('#form-active').onchange=e=>{editor.active=e.target.checked;formDirty=true};
 $('#new-template').onclick=()=>{if(formDirty&&!confirm('捨棄尚未發布的修改？'))return;editor=null;formDirty=false;renderAdmin()};
 document.querySelectorAll('[data-template]').forEach(b=>b.onclick=()=>{if(formDirty&&!confirm('捨棄尚未發布的修改？'))return;editor=editorFrom(item(+b.dataset.template));formDirty=false;renderAdmin()});
 document.querySelectorAll('[data-add-field]').forEach(b=>b.onclick=()=>{editor.fields.push({key:'field_'+crypto.randomUUID().replaceAll('-',''),label:'',type:b.dataset.addField,required:false,...(['select','multi'].includes(b.dataset.addField)?{options:['選項一','選項二']}:{})});formDirty=true;drawFieldEditor()});
 $('#preview-form').onclick=previewForm;
 $('#publish-form').onclick=()=>action(async()=>{
  if(!editor.name.trim())return toast('請填寫表單名稱');
  const invalid=editor.fields.find(f=>!f.label.trim()||(['select','multi'].includes(f.type)&&(!f.options?.length||f.options.some(v=>!v.trim())||new Set(f.options.map(v=>v.trim())).size!==f.options.length)));
  if(invalid)return toast('請填妥欄位名稱，選項不可空白或重複');
  const button=$('#publish-form');button.disabled=true;
  try{const result=await api('item',editor);formDirty=false;state=await api('state');editor=editorFrom(item(result.id));renderAdmin();toast(`已發布表單 v${result.version}，可到資料集管理指定使用`)}finally{if(button.isConnected)button.disabled=false}
 });drawFieldEditor();
}
function drawFieldEditor(){
 $('#field-editor').innerHTML=editor.fields.length?editor.fields.map((f,index)=>`<article class="field-card"><div class="field-header"><strong>欄位 ${index+1}</strong><button type="button" data-up="${index}" ${index===0?'disabled':''} aria-label="欄位 ${index+1} 上移">↑</button><button type="button" data-down="${index}" ${index===editor.fields.length-1?'disabled':''} aria-label="欄位 ${index+1} 下移">↓</button><button type="button" class="danger" data-remove-field="${index}">刪除欄位</button></div><div class="row"><div><label for="field-label-${index}">欄位名稱</label><input id="field-label-${index}" data-field-label="${index}" maxlength="100" value="${esc(f.label)}" placeholder="例如 現場狀況"></div><div><label for="field-type-${index}">欄位類型</label><select id="field-type-${index}" data-field-type="${index}">${fieldTypes.map(([type,label])=>`<option value="${type}" ${f.type===type?'selected':''}>${label}</option>`).join('')}</select></div><label class="check required-field"><input data-field-required="${index}" type="checkbox" ${f.required?'checked':''}>必填</label></div>
 ${['select','multi'].includes(f.type)?`<div class="field-options"><label>下拉／多選選項</label>${(f.options||[]).map((value,j)=>`<div class="option-row"><input aria-label="欄位 ${index+1} 選項 ${j+1}" data-option-field="${index}" data-option-index="${j}" value="${esc(value)}" maxlength="100" placeholder="輸入選項文字"><button type="button" data-remove-option="${index}:${j}" aria-label="刪除欄位 ${index+1} 選項 ${j+1}">移除</button></div>`).join('')}<button type="button" data-add-option="${index}">＋ 新增選項</button></div>`:''}</article>`).join(''):'<div class="empty">從下方選擇欄位類型，開始建立表單。也可發布只有系統欄位的表單。</div>';
 document.querySelectorAll('[data-field-label]').forEach(x=>x.oninput=e=>{editor.fields[+x.dataset.fieldLabel].label=e.target.value;formDirty=true});
 document.querySelectorAll('[data-field-required]').forEach(x=>x.onchange=e=>{editor.fields[+x.dataset.fieldRequired].required=e.target.checked;formDirty=true;$('#form-message').textContent=`${editor.fields.length} 個自訂欄位 · ${editor.fields.filter(f=>f.required).length} 個必填`});
 document.querySelectorAll('[data-field-type]').forEach(x=>x.onchange=e=>{const f=editor.fields[+x.dataset.fieldType];f.type=e.target.value;f.key='field_'+crypto.randomUUID().replaceAll('-','');if(['select','multi'].includes(f.type))f.options=f.options||['選項一'];else delete f.options;formDirty=true;drawFieldEditor()});
 document.querySelectorAll('[data-remove-field]').forEach(b=>b.onclick=()=>{editor.fields.splice(+b.dataset.removeField,1);formDirty=true;drawFieldEditor()});
 for(const [attr,delta] of [['up',-1],['down',1]])document.querySelectorAll('[data-'+attr+']').forEach(b=>b.onclick=()=>{const i=+b.dataset[attr];[editor.fields[i],editor.fields[i+delta]]=[editor.fields[i+delta],editor.fields[i]];formDirty=true;drawFieldEditor()});
 document.querySelectorAll('[data-option-field]').forEach(x=>x.oninput=e=>{editor.fields[+x.dataset.optionField].options[+x.dataset.optionIndex]=e.target.value;formDirty=true});
 document.querySelectorAll('[data-add-option]').forEach(b=>b.onclick=()=>{editor.fields[+b.dataset.addOption].options.push('');formDirty=true;drawFieldEditor()});
 document.querySelectorAll('[data-remove-option]').forEach(b=>b.onclick=()=>{const [i,j]=b.dataset.removeOption.split(':').map(Number);editor.fields[i].options.splice(j,1);formDirty=true;drawFieldEditor()});
 $('#form-message').textContent=`${editor.fields.length} 個自訂欄位 · ${editor.fields.filter(f=>f.required).length} 個必填`;
}
function previewForm(){
 const m=document.createElement('div');m.className='form-preview-backdrop';m.innerHTML=`<section class="card form-preview" role="dialog" aria-modal="true" aria-label="表單預覽"><div class="toolbar"><h2>${esc(editor.name||'未命名表單')}</h2><button id="close-preview">關閉預覽</button></div><p class="muted">預覽不會儲存照片資料。</p><label>拍攝時間（系統欄位）</label><input disabled placeholder="由照片擷取"><label>經緯度（系統欄位）</label><input disabled placeholder="由照片擷取">${fields({fields:editor.fields,attrs:{}})}</section>`;document.body.append(m);$('#close-preview').onclick=()=>m.remove();m.onclick=e=>{if(e.target===m)m.remove()};$('#close-preview').focus();
}
function renderDatasets(departments){
 const dept=Number(sessionStorage.getItem('admin-dataset-dept'))||departments[0].id,chosen=departments.some(d=>d.id===dept)?dept:departments[0].id;
 const templates=state.items.filter(i=>i.department_id===chosen&&i.active),datasets=state.datasets.filter(d=>item(d.item_id)?.department_id===chosen);
 $('#main').innerHTML=`<div class="toolbar"><div><h1>資料集管理</h1><p class="muted">指定表單、修改名稱，或封存完成的資料集。</p></div><select id="dataset-department" aria-label="資料集所屬科室">${option(departments,chosen)}</select></div><div class="card"><h3>建立資料集</h3><form id="admin-create-dataset" class="dataset-create"><div><label for="new-dataset-name">資料集名稱</label><input id="new-dataset-name" name="name" required maxlength="120" placeholder="例如 道路巡查"></div><div><label for="new-dataset-form">使用表單</label><select id="new-dataset-form" name="item" required>${templates.length?option(templates,''):'<option value="">請先發布此科室表單</option>'}</select></div><button class="primary" ${templates.length?'':'disabled'}>建立資料集</button></form></div><div class="dataset-admin-grid">${datasets.map(d=>{const t=item(d.item_id),cnt=(state.summary?.dataset_counts?.[String(d.id)]??0);return `<section class="card dataset-card"><div class="toolbar"><h3>${esc(d.name)}</h3><span class="badge ${d.archived?'warn':''}">${d.archived?'封存':'使用中'}</span></div><p class="muted">${cnt} 張可見照片 · ${esc(dep(chosen)?.name)}</p><form data-edit-dataset="${d.id}"><label>資料集名稱<input name="name" required maxlength="120" value="${esc(d.name)}"></label><button type="submit">儲存名稱</button></form><form data-assign-dataset="${d.id}"><label>指定表單<select name="item" ${d.archived?'disabled':''}>${option(state.items.filter(i=>i.department_id===chosen&&(i.active||i.id===t.id)),t.id)}</select></label><p class="muted">目前 ${esc(t.name)} · v${t.version}${t.active?'':' · 已停用'}<br>更換後僅影響新照片，歷史與草稿保持原表單。</p><button type="submit" class="primary" ${d.archived?'disabled':''}>套用表單</button></form><button data-archive-dataset="${d.id}" style="margin-top:16px">${d.archived?'重新開放':'封存資料集'}</button></section>`}).join('')||'<div class="empty">此科室尚無資料集。</div>'}</div>`;
 $('#dataset-department').onchange=e=>{sessionStorage.setItem('admin-dataset-dept',e.target.value);renderAdmin()};
 $('#admin-create-dataset').onsubmit=e=>{e.preventDefault();action(async()=>{await api('dataset',Object.fromEntries(new FormData(e.target)));await refresh();toast('資料集已建立並指定表單')})};
 document.querySelectorAll('[data-edit-dataset]').forEach(f=>f.onsubmit=e=>{e.preventDefault();action(async()=>{const d=ds(+f.dataset.editDataset);await api('rename',{id:d.id,version:d.version,name:new FormData(f).get('name')});await refresh();toast('已更新名稱')})});
 document.querySelectorAll('[data-assign-dataset]').forEach(f=>f.onsubmit=e=>{e.preventDefault();action(async()=>{const d=ds(+f.dataset.assignDataset),target=+new FormData(f).get('item');await api('assign_form',{id:d.id,version:d.version,item:target});await refresh();toast('表單已指定，既有照片與草稿不變')})});
 document.querySelectorAll('[data-archive-dataset]').forEach(b=>b.onclick=()=>action(async()=>{await api('archive',{id:+b.dataset.archiveDataset});await refresh()}));
}
 function renderAccounts(){
 const bc=state.burst_config||{gap_seconds:5,dhash_max:14,span_seconds:15};
 $('#main').innerHTML=`<h1>帳號與設定</h1><div class="card"><h3>建立科室</h3><form id="depform"><input name="name" required placeholder="科室名稱"><button>新增科室</button></form></div><div class="card"><h3>人員列表</h3><table class="table">${state.users.map(u=>`<tr><td>${esc(u.username)}</td><td>${u.is_superuser?'系統管理者':state.memberships.filter(m=>m.user_id==u.id).map(m=>esc(dep(m.department_id)?.name)+' / '+esc(m.role)).join('、')}</td><td>${u.is_active?'啟用':'停用'}</td><td><button data-active="${u.id}">${u.is_active?'停用':'啟用'}</button></td></tr>`).join('')}</table><h3>新增帳號</h3><form id="userform" class="row"><input name="username" required placeholder="帳號"><input name="password" type="password" required placeholder="密碼"><select name="department">${option(state.departments,'')}</select><select name="role"><option value="member">同仁</option><option value="supervisor">主管</option><option value="viewer">僅閱覽</option></select><button>建立帳號</button></form><h3>科室角色授權</h3><form id="grant" class="row"><select name="user">${option(state.users,'','username')}</select><select name="department">${option(state.departments,'')}</select><select name="role"><option value="member">同仁</option><option value="supervisor">主管</option><option value="viewer">僅閱覽</option></select><button>儲存角色</button></form></div><div class="card"><h3>系統參數與連拍分組設定</h3><form id="config"><div class="row"><div><label>附近比對半徑（公尺，≥100）</label><input name="radius" type="number" min="100" value="${state.radius}"></div><div><label>連拍相鄰時間差（秒，1–300）</label><input name="burst_gap_seconds" type="number" min="1" max="300" value="${bc.gap_seconds}"></div><div><label>連拍整組最大跨度（秒，1–3600）</label><input name="burst_span_seconds" type="number" min="1" max="3600" value="${bc.span_seconds}"></div><div><label>dHash 距離門檻（0–32）</label><input name="burst_dhash_max" type="number" min="0" max="32" value="${bc.dhash_max}"></div></div><button style="margin-top:12px">儲存設定</button></form><hr style="margin:16px 0"><div class="row"><select id="burst-recompute-dataset"><option value="">全部資料集</option>${state.datasets.map(d=>`<option value="${d.id}">${esc(dep(item(d.item_id)?.department_id)?.name)}／${esc(d.name)}</option>`).join('')}</select><button type="button" id="burst-recompute-btn">依目前參數重新計算連拍群組</button></div></div><details class="card"><summary>最近 100 筆稽核紀錄</summary><table class="table">${state.audits.map(a=>`<tr><td>${esc(a.created)}</td><td>${esc(a.actor__username)}</td><td>${esc(a.action)}</td><td>${esc(a.target)}</td></tr>`).join('')}</table></details>`;
 for(const [id,path] of [['depform','department'],['userform','user'],['grant','membership'],['config','config']])$('#'+id).onsubmit=e=>{e.preventDefault();action(async()=>{await api(path,Object.fromEntries(new FormData(e.target)));await refresh();toast('已儲存')})};
 if($('#burst-recompute-btn'))$('#burst-recompute-btn').onclick=()=>action(async()=>{const dsId=$('#burst-recompute-dataset').value;const r=await api('burst_recompute',dsId?{dataset:+dsId}:{});await refresh();toast(`已重算 ${r.processed} 張照片，共 ${r.groups} 組連拍群組`)});
 document.querySelectorAll('[data-active]').forEach(b=>b.onclick=()=>action(async()=>{const u=state.users.find(x=>x.id==b.dataset.active);await api('membership',{user:u.id,active:!u.is_active});await refresh()}));
 if(typeof enhanceAccountsAdmin==='function')enhanceAccountsAdmin();
}
window.addEventListener('beforeunload',e=>{if(formDirty){e.preventDefault();e.returnValue=''}});

async function renderOperations(){
 $('#main').innerHTML='<h1>部署與維運</h1><p role="status">正在檢查資料庫與照片儲存區…</p>';
 try{
  const response=await fetch('/ops/status');const data=await response.json();if(!response.ok)throw new Error(data.error||'讀取失敗');
  if(tab!=='admin'||adminSection!=='operations')return;
  const service=data.service,backup=data['last-backup'],attempt=data['backup-attempt'];
  const date=value=>value?new Date(value*1000).toLocaleString('zh-TW'):'尚無紀錄';
  $('#main').innerHTML=`<div class="toolbar"><div><h1>部署與維運</h1><p class="muted">檢查儲存區識別、讀寫與資料庫連線。備份與還原由維運指令執行。</p></div><button id="ops-refresh">重新檢查</button></div><div class="ops-grid"><div class="card"><h2>${data.ready?'服務就緒':'服務需要處理'}</h2><p>資料庫：${data.checks.database?'正常':'無法連線'} · 照片儲存：${data.checks.storage?'正常':'無法使用'}</p><p>部署設定：${data.deployment_profile?'已啟用':'目前為本機開發模式；請依維運手冊啟動部署版'}</p>${data.media?`<p>儲存目錄：${esc(data.media)}</p><p>資料庫：${esc(data.database)}</p><p>儲存區剩餘：${data.free_bytes===null?'未知':(data.free_bytes/1024**3).toFixed(1)+' GB'}</p>`:''}</div><div class="card"><h2>服務監督</h2><p>${!service?'尚無監督紀錄':service.stopped?'已停止':service.stale?'心跳逾時，請檢查服務':'運作中'}</p>${service?`<p>更新：${date(service.updated)}</p><p>${Object.entries(service.children||{}).map(([name,item])=>`${esc(name)}：${item.running?'運作中':'已結束'}（重新啟動 ${item.restarts||0} 次）`).join(' · ')}</p>`:''}</div>${Object.keys(data.workers||{}).length?`<div class="card"><h2>工作程序心跳</h2>${Object.entries(data.workers).map(([name,item])=>`<p>${esc(name)}：${item.stale?'心跳逾時':esc(({idle:'待命',working:'處理中',error:'需要處理',unknown:'尚未回報'})[item.state]||item.state)} · ${date(item.updated)}</p>`).join('')}</div>`:''}<div class="card"><h2>最近成功備份</h2>${data.backup_overdue?'<p class="muted">備份已逾期或尚未建立，請檢查儲存空間與維運記錄。</p>':''}${attempt&&!attempt.success?`<p>最近嘗試失敗：${date(attempt.created)}，先前成功備份仍保留。</p>`:''}<p>${backup?`${date(backup.created)} · 已通過雜湊與資料庫完整性驗證`:'尚無已驗證的備份'}</p>${backup?`<p>${esc(backup.archive)}</p>`:''}<p class="muted">還原只能輸出到全新目錄，確認後再由維運人員切換設定。網路芳鄰須以部署帳號另行驗證實際權限。</p></div></div><div id="admin-boundary-panel"></div><div id="admin-reconcile-panel"></div>`;
  $('#ops-refresh').onclick=renderOperations;
  if(state.user.admin){loadBoundaryAdminPanel();loadReconcileAdminPanel()}
 }catch(error){if(tab==='admin'&&adminSection==='operations')$('#main').innerHTML=`<h1>部署與維運</h1><div class="card">${esc(error.message)}</div>`}
}
async function loadBoundaryAdminPanel(){
 const wrap=$('#admin-boundary-panel');if(!wrap)return;
 try{
  const r=await api('boundary/list',{});if(!$('#admin-boundary-panel'))return;
  const sc=r.status_counts||{};
  wrap.innerHTML=`<section class="card" style="margin-top:16px"><div class="toolbar"><h2>行政區邊界資料版本管理</h2><button type="button" id="boundary-recompute-btn" class="primary">依啟用版本排程批次重算</button></div><p class="muted">反查狀態統計：成功 ${sc.ok||0} · 無座標 ${sc.no_coords||0} · 超出範圍 ${sc.out_of_bounds||0} · 查無行政區 ${sc.not_found||0} · 邊界不可用 ${sc.boundary_unavailable||0} · 失敗 ${sc.failed||0}</p><table class="table"><thead><tr><th>版本</th><th>來源與授權</th><th>發布日／座標系統</th><th>SHA-256（前16碼）</th><th>狀態／啟用時間</th><th>操作</th></tr></thead><tbody>${(r.versions||[]).map(v=>`<tr><td><strong>${esc(v.version)}</strong><br><small>${esc(v.file_path)}</small></td><td>${esc(v.source)}</td><td>${esc(v.published_date||'-')}<br><small>${esc(v.crs)}</small></td><td><code>${esc((v.sha256||'').slice(0,16))}</code></td><td>${v.active?'<span class="badge">啟用中</span>':'備用'}<br><small>${esc(v.activated_at||'')}</small></td><td>${v.active?'':`<button type="button" data-activate-boundary="${esc(v.version)}">設為啟用</button>`}</td></tr>`).join('')}</tbody></table></section>`;
  wrap.querySelectorAll('[data-activate-boundary]').forEach(b=>b.onclick=()=>action(async()=>{await api('boundary/activate',{version:b.dataset.activateBoundary});toast('已切換啟用行政區邊界版本');loadBoundaryAdminPanel()}));
  $('#boundary-recompute-btn').onclick=()=>action(async()=>{const res=await api('jobs/create',{kind:'region_recompute',only_failed:false});toast(`已排程行政區批次重算工作（${res.job.id.slice(0,8)}），請至工作中心查看`)});
 }catch{}
}
async function loadReconcileAdminPanel(){
 const wrap=$('#admin-reconcile-panel');if(!wrap)return;
 try{
  const [cap,reps]=await Promise.all([api('reconcile/capacity',{}),api('reconcile/reports',{})]);
  if(!$('#admin-reconcile-panel'))return;
  const mb=b=>(b/1048576).toFixed(2)+' MB';
  const latest=(reps.reports||[])[0];
  const counts=latest?.summary?.counts||{};
  wrap.innerHTML=`<section class="card" style="margin-top:16px"><div class="toolbar"><h2>檔案對帳與容量統計</h2><div class="row" style="flex:0 0 auto"><button type="button" id="reconcile-run-btn" class="primary">立即執行對帳報告（不刪除檔案）</button><button type="button" id="reconcile-repair-btn">修復缺漏縮圖</button></div></div><p class="muted">剩餘空間：${cap.free_bytes==null?'未知':(cap.free_bytes/1073741824).toFixed(2)+' GB'} · 未處理異常：${cap.unresolved_anomalies} 件 · 匯出包：${mb(cap.derived_bytes?.exports_bytes||0)} · 人物參考：${mb(cap.derived_bytes?.people_ref_bytes||0)} · 外部暫存：${mb(cap.derived_bytes?.external_temp_bytes||0)}</p><table class="table"><thead><tr><th>科室／資料集</th><th>正式</th><th>草稿</th><th>回收</th><th>原圖容量</th><th>縮圖容量</th></tr></thead><tbody>${(cap.datasets||[]).map(d=>`<tr><td>${esc(d.department_name)}／${esc(d.name)}</td><td>${d.submitted}</td><td>${d.draft}</td><td>${d.recycled}</td><td>${mb(d.original_bytes)}</td><td>${mb(d.preview_bytes)}</td></tr>`).join('')}</tbody></table>${latest?`<div style="margin-top:14px"><h3>最近對帳報告（${esc(latest.created)} · 寬限 ${latest.grace_hours} 小時）</h3><p class="muted">原圖遺失 ${counts.missing_original||0} · 縮圖異常 ${counts.missing_preview||0} · 孤立檔案 ${counts.orphan_file||0} · 雜湊不符 ${counts.hash_mismatch||0} · 語意缺漏 ${counts.semantic_gap||0} · OCR 缺漏 ${counts.ocr_gap||0} · 人臉缺漏 ${counts.face_gap||0} · 外部暫存逾時 ${counts.external_stale||0} · 逾時停滯工作 ${counts.stalled_job||0}${latest?.summary?.total_deferred?`（進行中暫緩 ${latest.summary.total_deferred} 項）`:''}</p></div>`:'<p class="muted" style="margin-top:10px">尚未執行對帳報告。</p>'}</section>`;
  $('#reconcile-run-btn').onclick=()=>action(async()=>{await api('reconcile/run',{grace_hours:24,verify_hashes:true});toast('已完成檔案對帳報告（未變更任何檔案）');loadReconcileAdminPanel()});
  $('#reconcile-repair-btn').onclick=()=>action(async()=>{const res=await api('reconcile/repair_previews',{});toast(`縮圖修復完成：成功 ${res.repaired} 張，失敗 ${res.failed} 張`);loadReconcileAdminPanel()});
 }catch{}
}

let intEditSystem=null,intAuditFilter={audit_system:'',audit_action:'',audit_outcome:'',audit_case_id:''};
function resetIntegrations(){intEditSystem=null;intAuditFilter={audit_system:'',audit_action:'',audit_outcome:'',audit_case_id:''}}

function openReasonModal({title,message,placeholder='請填寫操作理由（必填）',confirmLabel='確認執行',danger=false,onConfirm}){
 document.querySelector('.ux-dialog-backdrop')?.remove();
 const m=document.createElement('div');m.className='form-preview-backdrop ux-dialog-backdrop';
 m.innerHTML=`<section class="card ux-dialog" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="toolbar"><h2>${esc(title)}</h2><button type="button" data-ux-close>關閉</button></div>${message?`<p class="muted">${esc(message)}</p>`:''}<form id="ux-reason-form"><label for="ux-reason-input">異動理由（必填）</label><textarea id="ux-reason-input" required placeholder="${esc(placeholder)}"></textarea><div class="row" style="margin-top:14px"><button type="button" data-ux-close>取消</button><button type="submit" class="${danger?'danger':'primary'}">${esc(confirmLabel)}</button></div></form></section>`;
 document.body.append(m);
 const onKey=e=>{if(e.key==='Escape')close()};
 const close=()=>{window.removeEventListener('keydown',onKey);m.remove()};
 window.addEventListener('keydown',onKey);
 m.querySelectorAll('[data-ux-close]').forEach(b=>b.onclick=close);
 m.onclick=e=>{if(e.target===m)close()};
 $('#ux-reason-input')?.focus();
 $('#ux-reason-form').onsubmit=e=>{
  e.preventDefault();
  const reason=($('#ux-reason-input')?.value||'').trim();
  if(!reason)return;
  close();
  action(()=>onConfirm(reason));
 };
}

function isoToTaipeiLocal(iso){
 if(!iso)return '';
 const d=new Date(iso);
 if(Number.isNaN(d.getTime()))return '';
 return new Date(d.getTime()+8*3600000).toISOString().slice(0,16);
}

function formatTaipeiDateTime(iso,withSeconds=false){
 if(!iso)return '';
 const d=new Date(iso);
 if(Number.isNaN(d.getTime()))return String(iso);
 return new Date(d.getTime()+8*3600000).toISOString().slice(0,withSeconds?19:16).replace('T',' ');
}

function parseRetentionDaysInput(raw,label){
 const s=String(raw??'').trim();
 if(!/^\d+$/.test(s))throw Error(`${label}須為 0 至 3650 的整數天數`);
 const n=Number(s);
 if(!Number.isSafeInteger(n)||n<0||n>3650)throw Error(`${label}須介於 0 至 3650 天之間`);
 return n;
}

function taipeiLocalToIso(val){
 const s=String(val||'').trim();
 if(!s)return null;
 return s.length===16?`${s}:00+08:00`:`${s}+08:00`;
}

async function renderIntegrations(flashHtml=''){
 $('#main').innerHTML='<h1>串接系統管理</h1><p role="status">載入串接系統設定中…</p>';
 try{
  const data=await api('integration_admin/list',intAuditFilter);
  if(tab!=='admin'||adminSection!=='integrations')return;
  const capsList=[['search','一般搜尋'],['nearby','附近搜尋'],['ocr_search','OCR 搜尋'],['semantic_search','內容搜尋'],['thumbnail','縮圖'],['report_import','報告取圖'],['original_download','原圖下載'],['mcp','MCP 工具'],['picker','照片挑選器']];
  const ed=intEditSystem;
  const edDatasets=new Set((ed?.allowed_datasets||[]).map(Number));
  const edFields=ed?.allowed_fields||{captured:true,location:true,tags:true,custom_fields:{}};
  const dsFieldsMap=data.dataset_fields||{};
  const stats=data.stats||{};
  const cand=stats.cleanup_candidates||{};
  const mb=b=>`${((b||0)/1048576).toFixed(2)} MB`;

  const selectedCustomFields={};
  Object.entries(edFields.custom_fields||{}).forEach(([did,fids])=>{selectedCustomFields[String(did)]=new Set((fids||[]).map(String))});

  const syncCustomFieldsFromDom=root=>{
   if(!root)return;
   root.querySelectorAll('[data-cf-ds]').forEach(box=>{
    const did=box.dataset.cfDs;
    selectedCustomFields[did]=new Set([...box.querySelectorAll(`input[name="cf_${did}"]:checked`)].map(c=>c.value));
   });
  };

  const renderCustomFieldsHtml=selectedDsIds=>{
   const blocks=[];
   for(const did of selectedDsIds){
    const flist=dsFieldsMap[String(did)]||[];
    if(!flist.length)continue;
    const allowedSet=selectedCustomFields[String(did)]||new Set();
    blocks.push(`<div data-cf-ds="${did}" style="margin:4px 0"><span class="muted">${esc(ds(did)?.name||('#'+did))}：</span>${flist.map(f=>`<label class="check"><input type="checkbox" name="cf_${did}" value="${esc(f.id)}" ${allowedSet.has(f.id)?'checked':''}>${esc(f.label)} (${esc(f.id)})</label>`).join('')}</div>`);
   }
   return blocks.length?blocks.join(''):'<span class="muted">勾選授權資料集後，可於此逐欄開放允許回傳的自訂欄位（預設不開放）。</span>';
  };

  $('#main').innerHTML=`<div class="toolbar"><div><h1>串接系統管理（API / MCP / 挑選器）</h1><p class="muted">雙層授權：串接系統範圍 ∩ 操作人員權限。變更均保留理由與前後紀錄。</p></div><div class="row" style="flex:0 0 auto"><a class="button" href="/integration/v1/docs" target="_blank" rel="noopener">API 文件</a><a class="button" href="/integration/v1/openapi.json" target="_blank" rel="noopener">OpenAPI JSON</a><a class="button" href="/integration-demo" target="_blank" rel="noopener">防災整合示範工作台</a></div></div>
  ${flashHtml?`<div id="int-flash-out">${flashHtml}</div>`:'<div id="int-flash-out"></div>'}
  <section class="card"><div class="toolbar"><h3 style="margin:0">${ed?`編輯串接系統：${esc(ed.code)}`:'新增串接系統'}</h3>${ed?'<button type="button" id="int-cancel-edit">取消編輯（切換為新增）</button>':''}</div>
  <form id="int-create-form"><div class="row"><input name="code" required value="${esc(ed?.code||'')}" placeholder="系統識別碼（如 disaster-response）"><input name="name" required value="${esc(ed?.name||'')}" placeholder="系統名稱（如 防災系統）"><input name="owner_unit" value="${esc(ed?.owner_unit||'')}" placeholder="負責單位"><input name="contact" value="${esc(ed?.contact||'')}" placeholder="聯絡資訊"></div>
  <div style="margin:8px 0"><strong>授權資料集（明確指定，不隨科室自動擴大）：</strong><div class="row" style="flex-wrap:wrap">${state.datasets.map(d=>`<label class="check"><input type="checkbox" name="ds" value="${d.id}" ${edDatasets.has(d.id)?'checked':''}>${esc(d.name)}</label>`).join('')}</div></div>
  <div style="margin:8px 0"><strong>功能權限：</strong><div class="row" style="flex-wrap:wrap">${capsList.map(([k,l])=>`<label class="check"><input type="checkbox" name="cap_${k}" ${(ed?Boolean(ed.capabilities?.[k]):k!=='original_download')?'checked':''}>${l}</label>`).join('')}</div></div>
  <div style="margin:8px 0"><strong>可提供欄位：</strong><div class="row" style="flex-wrap:wrap"><label class="check"><input type="checkbox" name="af_captured" ${edFields.captured!==false?'checked':''}>拍攝時間</label><label class="check"><input type="checkbox" name="af_location" ${edFields.location!==false?'checked':''}>座標／行政區</label><label class="check"><input type="checkbox" name="af_tags" ${edFields.tags!==false?'checked':''}>標籤</label></div><div id="int-custom-fields-box" style="margin-top:6px;padding:8px;background:#f8fafc;border-radius:6px">${renderCustomFieldsHtml([...edDatasets])}</div></div>
  <div class="row" style="margin-top:8px"><input name="allowed_origins" value="${esc((ed?.allowed_origins||[window.location.origin]).join(', '))}" placeholder="允許來源網域（逗號分隔）"><input name="max_results_per_page" type="number" value="${ed?.max_results_per_page??50}" min="1" max="200" title="每頁上限" placeholder="每頁上限"><input name="max_import_count" type="number" value="${ed?.max_import_count??50}" min="1" max="200" title="匯入上限" placeholder="匯入上限"><input name="max_preview_per_call" type="number" value="${ed?.max_preview_per_call??12}" min="1" max="30" title="MCP 單次預覽上限" placeholder="預覽上限"><input name="rate_limit_per_minute" type="number" value="${ed?.rate_limit_per_minute??120}" min="1" max="1000" title="每分鐘頻率上限" placeholder="次/分"></div>
  <div class="row" style="margin-top:8px"><label style="flex:1">啟用時間（臺灣時間，可留空）<input name="valid_from" type="datetime-local" value="${esc(isoToTaipeiLocal(ed?.valid_from))}"></label><label style="flex:1">到期時間（臺灣時間，可留空）<input name="valid_until" type="datetime-local" value="${esc(isoToTaipeiLocal(ed?.valid_until))}"></label><label class="check" style="align-self:flex-end"><input type="checkbox" name="active" ${(ed?ed.active:true)?'checked':''}>啟用中</label></div>
  <div class="row" style="margin-top:8px"><label class="check"><input type="checkbox" name="allow_service_token" ${ed?.allow_service_token?'checked':''}>允許純背景服務身分憑證（client_credentials，不可用於互動式匯入或 MCP）</label><select name="service_user_id"><option value="">（未指定服務帳號）</option>${state.users.map(u=>`<option value="${u.id}" ${ed?.service_user_id==u.id?'selected':''}>服務帳號：${esc(u.username)}</option>`).join('')}</select></div>
  <div class="row" style="margin-top:8px"><input name="reason" required placeholder="${ed?'修改理由（必填）':'建立理由（必填）'}"><button class="primary" style="flex:0 0 auto">${ed?'儲存系統設定':'建立串接系統'}</button></div></form></section>

  <section class="card"><h3>已註冊串接系統</h3><table class="table"><thead><tr><th>識別碼／名稱</th><th>狀態／效期</th><th>授權資料集</th><th>上限</th><th>操作</th></tr></thead><tbody>${(data.systems||[]).map(s=>`<tr><td><strong>${esc(s.code)}</strong><br>${esc(s.name)}<br><small class="muted">${esc(s.owner_unit)} ${esc(s.contact)}</small></td><td>${s.revoked?'<span class="badge warn">已撤銷</span>':(s.active?'<span class="badge">啟用中</span>':'<span class="badge">停用</span>')}${s.valid_until?`<br><small class="muted">至 ${esc(formatTaipeiDateTime(s.valid_until))}</small>`:''}</td><td>${(s.allowed_datasets||[]).map(id=>esc(ds(id)?.name||('#'+id))).join('、')||'無'}</td><td>每頁 ${s.max_results_per_page}／匯入 ${s.max_import_count}／預覽 ${s.max_preview_per_call}／${s.rate_limit_per_minute} 次/分</td><td><div class="row" style="gap:6px;flex-wrap:wrap">${s.revoked?'':`<button type="button" data-edit-sys="${esc(s.id)}">編輯</button><button type="button" data-rotate-sys="${esc(s.id)}">輪換密鑰</button><button type="button" class="danger" data-revoke-sys="${esc(s.id)}">撤銷授權</button>`}</div></td></tr>`).join('')||'<tr><td colspan="5">尚無串接系統</td></tr>'}</tbody></table></section>

  <section class="card"><h3>外部人員身分綁定（OIDC / Identity Exchange）</h3><p class="muted">對應外部系統的穩定發行者（issuer）與使用者識別（subject）至本機歸檔帳號，取代不安全的外部自填 user_id。</p>
  <form id="int-bind-form" class="row"><select name="system_id">${(data.systems||[]).filter(s=>!s.revoked).map(s=>`<option value="${esc(s.id)}">${esc(s.code)}</option>`).join('')}</select><input name="issuer" required placeholder="發行者 issuer（如 https://sso.agency.gov.tw）"><input name="subject" required placeholder="外部使用者 subject（如 emp-1024）"><select name="user_id">${state.users.map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('')}</select><button class="primary" style="flex:0 0 auto">新增／更新綁定</button></form>
  <table class="table" style="margin-top:10px"><thead><tr><th>系統</th><th>發行者 (issuer)</th><th>外部識別 (subject)</th><th>對應本機帳號</th><th>狀態</th><th>操作</th></tr></thead><tbody>${(data.bindings||[]).map(b=>`<tr><td>${esc(b.system_code)}</td><td><code>${esc(b.issuer)}</code></td><td><code>${esc(b.subject)}</code></td><td>${esc(b.username)}</td><td>${b.active?'<span class="badge">啟用</span>':'<span class="badge warn">停用</span>'}</td><td><button type="button" data-toggle-bind="${b.id}" data-bind-next="${b.active?'0':'1'}">${b.active?'停用':'重新啟用'}</button></td></tr>`).join('')||'<tr><td colspan="6">尚無外部身分綁定</td></tr>'}</tbody></table></section>

  <section class="card"><h3>核發人員存取憑證（本機測試／整合驗證）</h3><form id="int-token-form" class="row"><select name="system_id">${(data.systems||[]).filter(s=>!s.revoked).map(s=>`<option value="${esc(s.id)}">${esc(s.code)} (${esc(s.name)})</option>`).join('')}</select><select name="user_id">${state.users.map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('')}</select><select name="audience"><option value="integration-api">integration-api (API + MCP + 挑選器)</option><option value="mcp">mcp (僅 MCP)</option></select><input name="ttl_minutes" type="number" value="60" min="5" max="1440" style="width:90px"><button class="primary">核發憑證</button></form><div id="int-token-out" style="margin-top:8px"></div>
  <h4 style="margin-top:14px">已核發憑證</h4><table class="table"><thead><tr><th>系統</th><th>操作人員</th><th>對象</th><th>到期</th><th>狀態</th><th>操作</th></tr></thead><tbody>${(data.tokens||[]).map(t=>`<tr><td>${esc(t.system_code)}</td><td>${esc(t.username)} (${esc(t.actor_type)})</td><td>${esc(t.audience)}</td><td>${esc(formatTaipeiDateTime(t.expires_at,true))}</td><td>${t.revoked?'已撤銷':'有效'}</td><td>${t.revoked?'':`<button type="button" data-revoke-tok="${esc(t.id)}">撤銷</button>`}</td></tr>`).join('')||'<tr><td colspan="6">尚無憑證</td></tr>'}</tbody></table></section>

  <section class="card"><h3>報告匯入作業追蹤（最近 ${(data.imports||[]).length} 筆）</h3><table class="table"><thead><tr><th>時間</th><th>系統／操作者</th><th>案件／報告</th><th>狀態</th><th>張數（成功／過期／失敗）</th></tr></thead><tbody>${(data.imports||[]).map(j=>`<tr><td>${esc(formatTaipeiDateTime(j.created,true))}</td><td>${esc(j.system_code)} / ${esc(j.username)}</td><td>${esc(j.case_id||'—')} / ${esc(j.report_id||'—')}</td><td><span class="badge ${j.state==='completed'?'':'warn'}">${esc(j.state)}</span></td><td>共 ${j.total}（${j.ready} / ${j.expired} / ${j.failed}）</td></tr>`).join('')||'<tr><td colspan="5">尚無報告匯入紀錄</td></tr>'}</tbody></table></section>

  <section class="card"><h3>報告副本儲存與生命週期維護</h3><p class="muted">作用中系統：${stats.systems_active||0}/${stats.systems_total||0} · 有效憑證：${stats.tokens_active||0} · 有效挑選工作階段：${stats.picker_sessions_active||0} · 報告副本檔案：${stats.report_storage_files||0} 檔（${mb(stats.report_storage_bytes)}）· 待清理候選：報告檔 ${cand.expired_report_files||0}、授權碼 ${cand.expired_auth_codes||0}、憑證 ${cand.expired_tokens||0}、工作階段 ${cand.expired_picker_sessions||0}、逾齡稽核字詞 ${cand.redactable_audits||0}</p>
  <form id="int-cleanup-form" class="row"><label>報告副本保留天數<input name="report_retention_days" type="number" required value="30" min="0" max="3650"></label><label>過期憑證保留天數<input name="token_retention_days" type="number" required value="30" min="0" max="3650"></label><label>稽核字詞保留天數<input name="audit_detail_days" type="number" required value="180" min="0" max="3650"></label><button type="button" id="int-cleanup-dry">預覽清理 (Dry Run)</button><button type="button" id="int-cleanup-exec" class="danger">執行清理</button></form><div id="int-cleanup-out" style="margin-top:8px"></div></section>

  <details class="card"><summary>串接系統授權變更紀錄（${(data.events||[]).length} 筆）</summary><table class="table"><thead><tr><th>時間</th><th>系統</th><th>操作者</th><th>動作</th><th>理由</th></tr></thead><tbody>${(data.events||[]).map(ev=>`<tr><td>${esc(formatTaipeiDateTime(ev.created,true))}</td><td>${esc(ev.system_code)}</td><td>${esc(ev.actor)}</td><td>${esc(ev.action)}</td><td>${esc(ev.reason)}</td></tr>`).join('')||'<tr><td colspan="5">尚無變更紀錄</td></tr>'}</tbody></table></details>

  <details class="card" open><summary>最近整合稽核紀錄（${(data.audits||[]).length} 筆）</summary>
  <form id="int-audit-filter" class="row" style="margin:10px 0"><select name="audit_system"><option value="">全部系統</option>${(data.systems||[]).map(s=>`<option value="${esc(s.code)}" ${intAuditFilter.audit_system===s.code?'selected':''}>${esc(s.code)}</option>`).join('')}</select><input name="audit_action" value="${esc(intAuditFilter.audit_action)}" placeholder="動作（如 photos_search / report_import）"><select name="audit_outcome"><option value="">全部結果</option>${['success','denied','failed','partial_failed'].map(o=>`<option value="${o}" ${intAuditFilter.audit_outcome===o?'selected':''}>${o}</option>`).join('')}</select><input name="audit_case_id" value="${esc(intAuditFilter.audit_case_id)}" placeholder="案件編號篩選"><button class="primary" style="flex:0 0 auto">篩選稽核</button></form>
  <table class="table"><thead><tr><th>時間</th><th>系統／人員</th><th>通道</th><th>動作</th><th>結果</th><th>案件／報告</th></tr></thead><tbody>${(data.audits||[]).map(a=>`<tr><td>${esc(formatTaipeiDateTime(a.created,true))}</td><td>${esc(a.system_code)} / ${esc(a.username)}</td><td>${esc(a.channel)}</td><td>${esc(a.action)}</td><td>${esc(a.outcome)} ${esc(a.error_code)}</td><td>${esc(a.case_id)} ${esc(a.report_id)}</td></tr>`).join('')||'<tr><td colspan="6">查無符合條件的稽核紀錄</td></tr>'}</tbody></table></details>`;

  const form=$('#int-create-form');
  form.querySelectorAll('input[name="ds"]').forEach(cb=>cb.onchange=()=>{
   syncCustomFieldsFromDom($('#int-custom-fields-box'));
   const sel=[...form.querySelectorAll('input[name="ds"]:checked')].map(c=>+c.value);
   $('#int-custom-fields-box').innerHTML=renderCustomFieldsHtml(sel);
  });
  if($('#int-cancel-edit'))$('#int-cancel-edit').onclick=()=>{intEditSystem=null;renderIntegrations()};

  form.onsubmit=e=>{e.preventDefault();action(async()=>{
   const f=e.target,fd=new FormData(f);
   const allowed_datasets=[...f.querySelectorAll('input[name="ds"]:checked')].map(c=>+c.value);
   const capabilities={};capsList.forEach(([k])=>{capabilities[k]=Boolean(f.querySelector(`input[name="cap_${k}"]`)?.checked)});
   const custom_fields={};
   for(const did of allowed_datasets){
    const chosen=[...f.querySelectorAll(`input[name="cf_${did}"]:checked`)].map(c=>c.value);
    if(chosen.length)custom_fields[String(did)]=chosen;
   }
   const allowed_fields={
    captured:Boolean(f.querySelector('input[name="af_captured"]')?.checked),
    location:Boolean(f.querySelector('input[name="af_location"]')?.checked),
    tags:Boolean(f.querySelector('input[name="af_tags"]')?.checked),
    custom_fields,
   };
   const origins=String(fd.get('allowed_origins')||'').split(',').map(x=>x.trim()).filter(Boolean);
   const payload={
    code:fd.get('code'),
    name:fd.get('name'),
    owner_unit:fd.get('owner_unit'),
    contact:fd.get('contact'),
    allowed_datasets,
    capabilities,
    allowed_fields,
    allowed_origins:origins,
    max_results_per_page:+fd.get('max_results_per_page'),
    max_import_count:+fd.get('max_import_count'),
    max_preview_per_call:+fd.get('max_preview_per_call'),
    rate_limit_per_minute:+fd.get('rate_limit_per_minute'),
    valid_from:taipeiLocalToIso(fd.get('valid_from')),
    valid_until:taipeiLocalToIso(fd.get('valid_until')),
    active:Boolean(f.querySelector('input[name="active"]')?.checked),
    allow_service_token:Boolean(f.querySelector('input[name="allow_service_token"]')?.checked),
    service_user_id:fd.get('service_user_id')?+fd.get('service_user_id'):null,
    reason:fd.get('reason'),
   };
   if(intEditSystem)payload.id=intEditSystem.id;
   const res=await api('integration_admin/save_system',payload);
   toast(intEditSystem?'串接系統設定已更新':'串接系統已建立');
   intEditSystem=null;
   const secretBanner=res.client_secret?`<div class="card" style="background:#fef3c7"><strong>系統密鑰（僅顯示一次，請妥善由業務系統後端保管）：</strong><code style="word-break:break-all">${esc(res.client_secret)}</code></div>`:'';
   await renderIntegrations(secretBanner);
  })};

  document.querySelectorAll('[data-edit-sys]').forEach(b=>b.onclick=()=>{
   intEditSystem=(data.systems||[]).find(x=>x.id===b.dataset.editSys)||null;
   renderIntegrations().then(()=>window.scrollTo({top:0,behavior:'smooth'}));
  });

  document.querySelectorAll('[data-rotate-sys]').forEach(b=>b.onclick=()=>{
   const sys=(data.systems||[]).find(x=>x.id===b.dataset.rotateSys);
   if(!sys)return;
   openReasonModal({
    title:`輪換串接系統密鑰（${sys.code}）`,
    message:'輪換後舊系統密鑰將立即失效，新密鑰僅顯示一次。',
    placeholder:'例如：定期密鑰輪換或人員異動',
    confirmLabel:'確認輪換密鑰',
    onConfirm:async reason=>{
     const res=await api('integration_admin/save_system',{...sys,rotate_secret:true,reason});
     toast('已完成密鑰輪換');
     const banner=res.client_secret?`<div class="card" style="background:#fef3c7"><strong>新系統密鑰（${esc(sys.code)}，僅顯示一次）：</strong><code style="word-break:break-all">${esc(res.client_secret)}</code></div>`:'';
     await renderIntegrations(banner);
    },
   });
  });

  document.querySelectorAll('[data-revoke-sys]').forEach(b=>b.onclick=()=>{
   const sys=(data.systems||[]).find(x=>x.id===b.dataset.revokeSys);
   openReasonModal({
    title:`撤銷串接系統授權（${sys?.code||b.dataset.revokeSys}）`,
    message:'撤銷後將立即停用該系統並作廢所有已核發憑證與挑選工作階段。',
    placeholder:'請填寫撤銷理由',
    confirmLabel:'確認撤銷授權',
    danger:true,
    onConfirm:async reason=>{
     await api('integration_admin/revoke_system',{id:b.dataset.revokeSys,reason});
     if(intEditSystem?.id===b.dataset.revokeSys)intEditSystem=null;
     toast('已撤銷串接系統與所有憑證');
     await renderIntegrations();
    },
   });
  });

  $('#int-bind-form').onsubmit=e=>{e.preventDefault();action(async()=>{
   const fd=new FormData(e.target);
   if(!fd.get('system_id')){toast('請先建立啟用中的串接系統');return}
   await api('integration_admin/bind_identity',{system_id:fd.get('system_id'),issuer:fd.get('issuer'),subject:fd.get('subject'),user_id:+fd.get('user_id'),active:true});
   toast('已儲存外部人員身分綁定');
   await renderIntegrations();
  })};

  document.querySelectorAll('[data-toggle-bind]').forEach(b=>b.onclick=()=>action(async()=>{
   await api('integration_admin/toggle_binding',{id:+b.dataset.toggleBind,active:b.dataset.bindNext==='1'});
   toast(b.dataset.bindNext==='1'?'已重新啟用外部身分綁定':'已停用外部身分綁定');
   await renderIntegrations();
  }));

  $('#int-token-form').onsubmit=e=>{e.preventDefault();action(async()=>{
   const fd=new FormData(e.target);
   if(!fd.get('system_id')){toast('請先建立啟用中的串接系統');return}
   const res=await api('integration_admin/issue_token',{system_id:fd.get('system_id'),user_id:+fd.get('user_id'),audience:fd.get('audience'),ttl_minutes:+fd.get('ttl_minutes')});
   toast('已核發存取憑證');
   const banner=`<div class="card" style="background:#e0f2fe"><strong>存取憑證（僅顯示一次）：</strong><code style="word-break:break-all">${esc(res.access_token)}</code></div>`;
   await renderIntegrations(banner);
  })};

  document.querySelectorAll('[data-revoke-tok]').forEach(b=>b.onclick=()=>action(async()=>{
   await api('integration_admin/revoke_token',{id:b.dataset.revokeTok});
   toast('已撤銷憑證');
   await renderIntegrations();
  }));

  const readCleanupRetentionPayload=()=>{
   const fd=new FormData($('#int-cleanup-form'));
   return {
    report_retention_days:parseRetentionDaysInput(fd.get('report_retention_days'),'報告副本保留天數'),
    token_retention_days:parseRetentionDaysInput(fd.get('token_retention_days'),'過期憑證保留天數'),
    audit_detail_days:parseRetentionDaysInput(fd.get('audit_detail_days'),'稽核字詞保留天數'),
   };
  };
  const executeCleanup=async(payload,dryRun)=>{
   const res=await api('integration_admin/cleanup',{...payload,dry_run:dryRun});
   const r=res.result;
   const failNote=r.failed_report_files?`（刪除失敗 ${r.failed_report_files} 檔，將於下次重試）`:'';
   const summary=`${r.dry_run?'[預覽 Dry Run] ':'[已執行清理] '}報告副本 ${r.expired_report_files} 檔（${mb(r.freed_bytes)}）${failNote}、授權碼 ${r.deleted_auth_codes}、憑證 ${r.deleted_tokens}、工作階段 ${r.deleted_picker_sessions}、稽核字詞遮罩 ${r.redacted_audits}`;
   if(dryRun){
    $('#int-cleanup-out').innerHTML=`<div class="card" style="background:#f1f5f9">${esc(summary)}</div>`;
   }else{
    toast('已完成整合生命週期清理');
    await renderIntegrations(`<div class="card" style="background:#dcfce7">${esc(summary)}</div>`);
   }
  };
  $('#int-cleanup-dry').onclick=()=>action(async()=>{
   const payload=readCleanupRetentionPayload();
   await executeCleanup(payload,true);
  });
  $('#int-cleanup-exec').onclick=()=>{
   let payload;
   try{payload=readCleanupRetentionPayload()}catch(e){toast(e.message);return}
   openConfirmDialog({
    title:'確認執行整合生命週期清理',
    message:`將依設定保留天數（報告 ${payload.report_retention_days} 天、憑證 ${payload.token_retention_days} 天、稽核字詞 ${payload.audit_detail_days} 天）清理逾齡報告實體副本（保留雜湊與追溯紀錄）、刪除過期憑證／工作階段，並遮罩逾齡稽核查詢字詞。`,
    confirmLabel:'確認清理',
    onConfirm:()=>executeCleanup(payload,false),
   });
  };

  $('#int-audit-filter').onsubmit=e=>{e.preventDefault();const fd=new FormData(e.target);intAuditFilter={audit_system:String(fd.get('audit_system')||''),audit_action:String(fd.get('audit_action')||'').trim(),audit_outcome:String(fd.get('audit_outcome')||''),audit_case_id:String(fd.get('audit_case_id')||'').trim()};renderIntegrations()};
 }catch(error){$('#main').innerHTML=`<h1>串接系統管理</h1><div class="card">${esc(error.message)}</div>`}
}
