const peopleUI={department:'',person:null,people:[],epoch:0,indexing:false};
function resetPeople(){peopleUI.epoch++;peopleUI.person=null;peopleUI.people=[];peopleUI.department='';peopleUI.indexing=false}
function facePicture(photo,bbox,label='人臉位置'){
 return `<div class="face-picture"><img src="${esc(photo.preview)}" alt="${esc(photo.name||label)}" loading="lazy">${bbox?`<span class="face-box" aria-label="${esc(label)}" style="left:${bbox[0]*100}%;top:${bbox[1]*100}%;width:${bbox[2]*100}%;height:${bbox[3]*100}%"></span>`:''}</div>`;
}
function renderPeople(){
 const s=peopleUI,epoch=++s.epoch;s.indexing=false;
 const departments=state.departments.filter(d=>state.user.capabilities?.[d.id]?.people_view);
 if(!departments.some(d=>d.id==s.department))s.department=departments[0]?.id||'';
 $('#side').innerHTML=`<h3>科室人物名冊</h3><p class="muted">人物閱覽、人臉搜尋與名冊維護分開授權。系統管理者也須另授。</p>${departments.length?`<label for="people-department">科室</label><select id="people-department">${option(departments,s.department)}</select><label for="people-filter">尋找姓名或內部編號</label><input id="people-filter" placeholder="可同名，以編號區分"><div id="people-roster"></div>`:'<p>尚未取得任何科室的人物閱覽權限。</p>'}`;
 $('#main').innerHTML='<div class="toolbar"><h1>名冊與離線人臉搜尋</h1><button id="people-refresh">重新整理</button></div><div id="people-message" role="status"></div><div id="person-workspace"></div>';
 if(!departments.length){$('#person-workspace').innerHTML=`<div class="empty">請由系統管理者在「管理後台 → 帳號與設定」另行授予本科室的專用權限。人物閱覽是使用入口的必要權限。${state.user.admin?'<p><button id="people-grants">開啟權限管理</button></p>':''}</div>`;if($('#people-grants'))$('#people-grants').onclick=()=>{tab='admin';adminSection='accounts';render()};return}
 const capabilities=state.user.capabilities[s.department]||{};
 function valid(){return epoch===s.epoch&&tab==='people'}
 $('#people-refresh').onclick=()=>renderPeople();$('#people-department').onchange=e=>{s.department=+e.target.value;s.person=null;renderPeople()};
 $('#people-filter').oninput=e=>document.querySelectorAll('[data-person]').forEach(b=>b.hidden=!b.textContent.includes(e.target.value));
 async function load(){
  const r=await api('people/list',{department:+s.department});if(!valid())return;s.people=r.people;
  $('#people-roster').innerHTML=(capabilities.people_manage?'<button id="person-new" class="primary">新增人物</button>':'')+s.people.map(p=>`<button data-person="${p.id}" class="${s.person===p.id?'primary':''}"><strong>${esc(p.name)}</strong><small>${esc(p.code)} · ${p.reference_count} 張參考${p.active?'':' · 已停用'}</small></button>`).join('');
  document.querySelectorAll('[data-person]').forEach(b=>b.onclick=()=>{s.person=b.dataset.person;renderPeople()});
  if($('#person-new'))$('#person-new').onclick=()=>{s.person=null;editor(null)};
  const person=s.people.find(p=>p.id===s.person);if(person)await workspace(person,r);else if(capabilities.people_manage)editor(null);else $('#person-workspace').innerHTML='<div class="empty">請從左側選擇人物。</div>';
 }
 function editor(p){
  $('#person-workspace').innerHTML=`<form id="person-editor" class="card"><h2>${p?'修改人物':'新增人物'}</h2><div class="row"><div><label for="person-name">姓名（可同名）</label><input id="person-name" required maxlength="100" value="${esc(p?.name||'')}"></div><div><label for="person-code">科室內部編號（唯一）</label><input id="person-code" required maxlength="60" value="${esc(p?.code||'')}"></div></div><label for="person-notes">備註</label><textarea id="person-notes" maxlength="2000">${esc(p?.notes||'')}</textarea>${p?`<label class="check"><input id="person-active" type="checkbox" ${p.active?'checked':''}>啟用人物</label>`:''}<button class="primary">儲存人物</button>${p?'<button id="person-cancel-edit" type="button">返回人物</button>':''}<p class="muted">停用會停止搜尋及顯示目前人物關聯，保留名冊、參考照片與人工確認歷史。</p></form>`;
  if($('#person-cancel-edit'))$('#person-cancel-edit').onclick=()=>renderPeople();
  $('#person-editor').onsubmit=e=>{e.preventDefault();action(async()=>{const r=await api('people/save',{department:+s.department,...(p?{id:p.id,version:p.version}:{}),name:$('#person-name').value,code:$('#person-code').value,notes:$('#person-notes').value,active:p?$('#person-active').checked:true});if(!valid())return;s.person=r.person.id;await load();toast('人物資料已保存')})};
 }
 async function workspace(p,metadata){
  $('#person-workspace').innerHTML=`<section class="card"><div class="toolbar"><h2>${esc(p.name)} <span class="badge">${esc(p.code)}</span> ${p.active?'':'<span class="badge warn">已停用</span>'}</h2>${p.can_manage?'<button id="person-edit">修改／停用人物</button>':''}</div><p>${esc(dep(p.department)?.name)} · ${esc(p.notes)}</p><small class="muted">人物 UUID：${esc(p.id)}</small><h3>參考照片（${p.reference_count} 張啟用）</h3><div class="reference-grid">${p.references.map(r=>`<article>${facePicture({preview:r.preview},null,'參考照片')}<a href="${r.preview}" target="_blank" rel="noopener">檢視參考照片</a><span class="badge ${r.active?'':'warn'}">${r.active?'啟用':'停用'}</span>${p.can_manage?`<button data-reference="${r.id}" data-active="${!r.active}">${r.active?'停用這張':'重新啟用'}</button>`:''}<small>清晰度 ${r.quality.blur} · 信心分數 ${r.quality.confidence}</small></article>`).join('')||'<p class="muted">沒有可顯示的參考照片，或目前權限僅允許閱覽人物姓名。</p>'}</div>${p.can_manage&&p.active?'<form id="person-upload"><label for="person-reference-files">新增多張參考照片（每批最多 5 張，每人最多 20 張啟用）</label><input id="person-reference-files" type="file" accept="image/jpeg,image/png,image/webp" multiple required><p class="muted">每張最多 10 MB，只接受單一清晰人臉；無臉、多臉、過小或模糊不會納入。</p><button class="primary">上傳並檢查參考照片</button></form><div id="reference-results" role="status"></div>':''}</section>${p.can_search&&p.active?`<section class="card"><h2>離線人臉搜尋</h2><label for="face-datasets">搜尋資料集（可複選；未選為本科室全部）</label><select id="face-datasets" multiple>${option(state.datasets.filter(d=>item(d.item_id)?.department_id===p.department),'')}</select><p id="face-coverage" role="status">正在確認索引…</p><div class="row"><button id="face-index">建立／繼續本範圍人臉索引</button><button id="face-retry">重試失敗索引</button><button id="face-stop" disabled>停止後續批次</button></div><p class="muted">只有按下索引才分析，每批 5 張。離開頁面或停止後，在目前批次完成處暫停；索引不建立人物關聯。</p><button id="face-search" class="primary">以此人物的啟用參考照片搜尋</button><p class="muted">模型 ${esc(metadata.model)} · 最低餘弦分數 ${metadata.minimum}。分數不是身分正確率；無法可靠偵測或品質不足，不會強制匹配。${metadata.model_ready?'模型檔案已就緒。':'模型尚未就緒，請先由管理者準備離線模型。'}</p></section>`:'<section class="card"><p>人物已停用，或尚未取得人臉搜尋權限；名冊閱覽不會自動開啟辨識。</p></section>'}<div id="face-error" role="alert"></div><div id="face-candidates"></div><section class="card"><h2>人工確認關聯與歷史</h2><div id="person-links">讀取中…</div></section>`;
  if($('#person-edit'))$('#person-edit').onclick=()=>editor(p);
  document.querySelectorAll('[data-reference]').forEach(b=>b.onclick=()=>action(async()=>{await api('people/reference-state',{person:p.id,version:p.version,reference:b.dataset.reference,active:b.dataset.active==='true'});if(valid())await load()}));
  if($('#person-upload'))$('#person-upload').onsubmit=e=>{e.preventDefault();action(async()=>{const fd=new FormData();fd.set('person',p.id);for(const f of $('#person-reference-files').files)fd.append('files',f);const button=e.target.querySelector('button');button.disabled=true;$('#reference-results').textContent='正在本機檢查人臉…';try{const r=await api('people/reference-upload',fd);if(!valid())return;await load();if($('#reference-results'))$('#reference-results').innerHTML=r.results.map(x=>`<p class="${x.ok?'':'error'}">${esc(x.name)}：${x.ok?'已加入參考照片':esc(x.error)}</p>`).join('')}finally{button.disabled=false}})};
  let searchSequence=0;
  function datasets(){return [...($('#face-datasets')?.selectedOptions||[])].map(o=>+o.value)}
  function progress(r){$('#face-coverage').textContent=`本科室可閱覽 ${r.total} 張 · 已索引 ${r.indexed} 張 · 待索引 ${r.pending} 張 · 失敗 ${r.failed} 張 · 可用人臉 ${r.faces} 張`}
  async function status(){const r=await api('people/status',{person:p.id,datasets:datasets()});if(valid())progress(r)}
  async function build(retry=false){
   if(s.indexing)return;s.indexing=true;$('#face-index').disabled=$('#face-retry').disabled=true;$('#face-stop').disabled=false;$('#face-datasets').disabled=true;
   try{while(valid()&&s.indexing){const r=await api('people/index',{person:p.id,datasets:datasets(),retry});retry=false;if(!valid())return;progress(r);if(!r.pending)break}}finally{if(valid()){s.indexing=false;$('#face-index').disabled=$('#face-retry').disabled=false;$('#face-stop').disabled=true;$('#face-datasets').disabled=false}}
  }
  if($('#face-index')){
   $('#face-index').onclick=()=>action(()=>build());$('#face-retry').onclick=()=>action(()=>build(true));$('#face-stop').onclick=()=>{s.indexing=false;$('#face-stop').disabled=true};$('#face-datasets').onchange=()=>{s.indexing=false;searchSequence++;$('#face-candidates').innerHTML='';action(status)};
   $('#face-search').onclick=()=>action(async()=>{const seq=++searchSequence,button=$('#face-search');button.disabled=true;$('#face-error').textContent='';try{const r=await api('people/search',{person:p.id,datasets:datasets()});if(!valid()||seq!==searchSequence)return;progress(r);candidates(r)}catch(error){if(valid())$('#face-error').textContent=error.message}finally{button.disabled=false}});
   await status();
  }
  if(valid())await links();
  function candidates(r){
   $('#face-candidates').innerHTML=`<p role="status">找到 ${r.matches} 張候選人臉，顯示 ${r.candidates.length} 張${r.pending||r.failed?'；索引涵蓋尚未完整':''}${r.limited?'；超過 500 張，請縮小資料集':''}。須逐一人工確認。</p><div class="face-candidate-grid">${r.candidates.map((c,i)=>`<article class="card">${facePicture(c.photo,c.bbox,'候選人臉 '+c.ordinal)}<h3>${esc(c.photo.name)} · 臉 ${c.ordinal}</h3><p>${esc(ds(c.photo.dataset)?.name)} · 餘弦分數 ${c.score.toFixed(3)}</p><button data-face-open="${i}">開啟完整照片</button>${c.linked?'<p class="badge">已人工確認關聯</p>':c.can_confirm?`<label for="face-reason-${i}">人工確認說明（必填）</label><textarea id="face-reason-${i}" maxlength="2000" placeholder="核對照片與參考照片後填寫"></textarea><button class="primary" data-face-confirm="${i}">確認此人臉為 ${esc(p.name)}</button>`:'<p class="muted">只有取得名冊維護權限者，才能確認關聯。封存照片不能新增關聯。</p>'}</article>`).join('')||'<div class="empty">沒有符合門檻的可用人臉候選。請確認參考照片、索引涵蓋及照片清晰度。</div>'}</div>`;
   document.querySelectorAll('[data-face-open]').forEach(b=>b.onclick=()=>action(async()=>{const c=r.candidates[+b.dataset.faceOpen];await openPhoto(c.photo.id)}));
   document.querySelectorAll('[data-face-confirm]').forEach(b=>b.onclick=()=>action(async()=>{const i=+b.dataset.faceConfirm,reason=$('#face-reason-'+i).value.trim();if(!reason)return toast('請填寫人工確認說明');b.disabled=true;try{await api('people/confirm',{token:r.candidates[i].token,reason});if(!valid())return;b.textContent='已人工確認關聯';await links();toast('已建立人物關聯並保存確認紀錄')}catch(error){b.disabled=false;throw error}}));
  }
  async function links(){
   const r=await api('people/links',{person:p.id});if(!valid())return;
   $('#person-links').innerHTML=r.links.length?r.links.map(l=>`<article class="person-link"><strong>${esc(l.photo.name)}</strong> <span class="badge ${l.active?'':'warn'}">${l.active?'有效關聯':'已撤銷'}</span>${l.stale?'<span class="badge warn">影像或模型已變動</span>':''}<details><summary>關聯事件紀錄（${l.events.length}）</summary>${l.events.map(e=>`<p>${e.action==='confirmed'?'人工確認':'撤銷'} · ${esc(e.actor)} · ${esc(compareDate(e.at))}<br>${esc(e.reason)}</p>`).join('')}</details>${l.active&&l.can_remove?`<label for="unlink-reason-${l.id}">撤銷理由</label><input id="unlink-reason-${l.id}" maxlength="2000"><button data-unlink="${l.id}" data-version="${l.version}">撤銷此人物關聯</button>`:''}</article>`).join(''):'<p class="muted">尚無人工確認關聯。辨識候選不會自動加入。</p>';
   document.querySelectorAll('[data-unlink]').forEach(b=>b.onclick=()=>action(async()=>{const reason=$('#unlink-reason-'+b.dataset.unlink).value.trim();if(!reason)return toast('請填撤銷理由');await api('people/unlink',{person:p.id,link:+b.dataset.unlink,version:+b.dataset.version,reason});if(valid()){await links();$('#face-candidates').innerHTML='<p>關聯已撤銷，請重新搜尋以更新候選狀態。</p>'}toast('已撤銷關聯，歷史紀錄保留')}));
  }
 }
 action(load);
}
