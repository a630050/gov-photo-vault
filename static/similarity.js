const similarityUI={dataset:'',run:null,offset:0,busy:false,epoch:0};
const similarityStates={pending:'尚未開始計算',running:'分析中（可繼續或取消）',completed:'已完成',cancelled:'已取消，沒有完整結果',failed:'失敗，請重新執行'};
const similarityReviews={unreviewed:'未檢閱',clear:'已檢閱，無須處理',anomaly:'已建立人工異常'};
function similarityAvailable(){return state.datasets.filter(d=>state.user.capabilities?.[item(d.item_id)?.department_id]?.analyze)}
function renderSimilarity(){
 const s=similarityUI,epoch=++s.epoch;s.busy=false;
 const datasets=similarityAvailable();if(!datasets.some(d=>d.id==s.dataset))s.dataset=datasets[0]?.id||'';
 $('#side').innerHTML=`<h3>資料集近似分析</h3><p class="muted">只有按下執行才分析。不會跨資料集，也不會在上傳後自動執行。</p><label for="sim-dataset">指定資料集</label><select id="sim-dataset">${datasets.map(d=>`<option value="${d.id}" ${d.id==s.dataset?'selected':''}>${esc(dep(item(d.item_id)?.department_id)?.name)}／${esc(d.name)}${d.archived?'（封存）':''}</option>`).join('')}</select><label for="sim-hours">拍攝時間差門檻（小時）</label><input id="sim-hours" type="number" min="0" max="876000" step="any" value="24"><p class="muted">嚴格超過此門檻才標示超時。24 小時即一天；缺時間或未確認時間需人工確認。</p><label class="check"><input id="sim-selected" type="checkbox">僅分析圖台已勾選照片</label><p id="sim-scope" role="status"></p><button id="sim-start" class="primary">手動執行近似分析</button><p class="muted">使用離線 dHash＋aHash 影像指紋，兩種距離皆須 ≤ 6／64。分數是指紋差異，不是造假機率。裁切、翻轉或明顯視角變化可能無法命中。</p>`;
 $('#main').innerHTML='<div class="toolbar"><h1>近似分析紀錄</h1><button id="sim-reload">重新整理</button></div><div id="sim-message" role="status"></div><div id="sim-history"></div><div id="sim-results"></div>';
 if(!datasets.length){$('#sim-start').disabled=true;$('#sim-message').textContent='尚未取得任何科室的近似分析權限，請由管理者授權。';return}
 $('#sim-dataset').onchange=e=>{s.dataset=+e.target.value;s.run=null;s.offset=0;renderSimilarity()};
 $('#sim-selected').onchange=scope;
 $('#sim-reload').onclick=()=>renderSimilarity();
 function scope(total){const chosen=state.photos.filter(p=>selected.has(p.id)&&p.dataset==s.dataset&&p.status==='submitted');$('#sim-scope').textContent=$('#sim-selected').checked?`本資料集已勾選 ${chosen.length} 張；其他資料集勾選不納入。`:`分析本資料集全部正式照片${typeof total==='number'?'，共 '+total+' 張':''}。本機單次最多 300 張。`;return chosen}
 $('#sim-start').onclick=()=>action(async()=>{
  if(s.busy)return;const hours=Number($('#sim-hours').value);if(!$('#sim-hours').value||!Number.isFinite(hours)||hours<0||hours>876000)return toast('請填寫有效的小時門檻');
  const chosen=scope();s.busy=true;$('#sim-start').disabled=true;
  try{const r=await api('similarity/start',{dataset:+s.dataset,hours,...($('#sim-selected').checked?{photos:chosen.map(p=>p.id)}:{})});s.run=r.id;s.offset=0;await work(r.id)}finally{if(epoch===s.epoch&&tab==='similarity'){s.busy=false;$('#sim-start').disabled=false}}
 });
 async function history(){
  const result=await api('similarity/list',{dataset:+s.dataset});if(epoch!==s.epoch||tab!=='similarity')return;
  scope(result.eligible);
  $('#sim-history').innerHTML=result.runs.length?`<div class="sim-history">${result.runs.map(r=>`<button data-sim-run="${r.id}" class="${s.run===r.id?'primary':''}">#${r.id} · ${esc(similarityStates[r.state])} · ${r.total} 張／${r.pairs} 配對<br><small>${esc(r.actor)} · ${esc(compareDate(r.created))} · ${r.hours} 小時${r.stale?' · 資料已變動':''}${r.failed?' · '+r.failed+' 張讀取失敗':''}</small></button>`).join('')}</div>`:'<div class="empty">尚無分析紀錄。請選擇資料集與門檻後手動執行。</div>';
  document.querySelectorAll('[data-sim-run]').forEach(b=>b.onclick=()=>action(async()=>{s.run=+b.dataset.simRun;s.offset=0;await results(s.run);await history()}));
 }
 async function work(id){
  $('#sim-start').disabled=true;s.busy=true;
  try{
   while(epoch===s.epoch&&tab==='similarity'&&s.run===id){
    const r=await api('similarity/step',{run:id});if(epoch!==s.epoch||tab!=='similarity'||s.run!==id)return;
    $('#sim-message').textContent=`分析 #${id}：${r.processed}／${r.total} 張已處理${r.failed?'，'+r.failed+' 張讀取失敗':''}`;
    await results(id);if(['completed','failed','cancelled'].includes(r.state))break;
   }
   if(epoch===s.epoch&&tab==='similarity'){await history()}
  }finally{if(epoch===s.epoch&&tab==='similarity'){s.busy=false;$('#sim-start').disabled=false}}
 }
 async function results(id){
  const result=await api('similarity/results',{run:id,offset:s.offset});if(epoch!==s.epoch||tab!=='similarity'||s.run!==id)return;
  const r=result.run;
  $('#sim-results').innerHTML=`<section class="card sim-run"><h2>分析 #${r.id} · ${esc(similarityStates[r.state])}</h2><p>${r.processed}／${r.total} 張 · ${r.pairs} 組命中配對 · 時間差嚴格超過 ${r.hours} 小時</p><p class="muted">${esc(r.algorithm)} · dHash ≤ ${r.thresholds.dhash}，aHash ≤ ${r.thresholds.ahash} · ${esc(r.actor)} · ${esc(compareDate(r.created))}</p>${r.stale?'<p class="metadata-warning">資料已變動，這是舊版本分析；請重新執行。已移動或無權查看的照片不顯示。</p>':''}${r.error?`<p class="error">${esc(r.error)}</p>`:''}${r.failed?`<p class="metadata-warning">${r.failed} 張原圖讀取失敗，結果只涵蓋成功讀取的照片。失敗照片 ID：${result.failures.map(x=>x.id).join('、')}</p>`:''}${['pending','running'].includes(r.state)?`<button id="sim-resume">繼續此分析</button><button id="sim-cancel" class="danger">取消分析</button><p class="muted">離開頁面會在目前批次完成後暫停，返回時可手動繼續。</p>`:''}<p class="muted">影像近似僅供檢閱。每個配對獨立判讀，不推論整組照片皆相同；新分析不沿用舊結論。</p></section><div class="sim-pairs">${result.pairs.map(pairHTML).join('')||(['completed'].includes(r.state)?'<div class="empty">沒有命中近似配對。</div>':'')}</div>${r.pairs>50?`<div class="row"><button id="sim-prev" ${s.offset?'':'disabled'}>上一頁</button><span>${s.offset+1}–${Math.min(s.offset+50,r.pairs)}／${r.pairs}</span><button id="sim-next" ${result.next===null?'disabled':''}>下一頁</button></div>`:''}`;
  if($('#sim-resume'))$('#sim-resume').onclick=()=>action(async()=>{if(!s.busy)await work(id)});
  if($('#sim-cancel'))$('#sim-cancel').onclick=()=>action(async()=>{await api('similarity/cancel',{run:id});await results(id);await history()});
  for(const [button,offset] of [['sim-prev',s.offset-50],['sim-next',s.offset+50]])if($('#'+button))$('#'+button).onclick=()=>action(async()=>{s.offset=Math.max(0,offset);await results(id)});
  document.querySelectorAll('[data-sim-compare]').forEach(b=>b.onclick=()=>action(async()=>{
   const fresh=await api('similarity/pair',{pair:+b.dataset.simCompare});if(epoch!==s.epoch||tab!=='similarity')return;
   const map=new Map(state.photos.map(p=>[p.id,p]));fresh.photos.forEach(p=>map.set(p.id,p));state.photos=[...map.values()];
   openPhoto(fresh.photos[0].id);openComparison(fresh.photos[0],{photo:fresh.photos[1],back:()=>action(async()=>{await results(id)})});
   if(fresh.stale)toast('照片資料已變動，請重新分析後標註');
  }));
  document.querySelectorAll('[data-sim-photo]').forEach(b=>b.onclick=()=>action(async()=>{const fresh=await api('similarity/pair',{pair:+b.dataset.pair});if(epoch!==s.epoch||tab!=='similarity')return;const map=new Map(state.photos.map(p=>[p.id,p]));fresh.photos.forEach(p=>map.set(p.id,p));state.photos=[...map.values()];openPhoto(+b.dataset.simPhoto)}));
  document.querySelectorAll('[data-sim-review]').forEach(b=>b.onclick=()=>action(async()=>{
   b.disabled=true;try{await api('similarity/review',{pair:+b.dataset.pair,review:b.dataset.simReview,note:$('#sim-note-'+b.dataset.pair).value,events:+b.dataset.events});await results(id);toast(b.dataset.simReview==='anomaly'?'兩張照片已建立待確認人工異常，可交主管確認或排除':'已保存配對檢閱紀錄')}finally{b.disabled=false}
  }));
 }
 function pairHTML(p){
  if(p.unavailable)return `<article class="card">配對 #${p.id}：照片已移動、回收或無權查看。</article>`;
  const time=p.time_state==='over'?'拍攝時間差超過門檻':p.time_state==='within'?'拍攝時間差未超過門檻':'拍攝時間需確認';
  return `<article class="card sim-pair"><h3>配對 #${p.id} <span class="badge ${p.time_state==='over'?'warn':''}">${time}</span></h3><div class="sim-photo-pair">${[p.a,p.b].map(x=>`<div><img src="${x.preview}" alt="${esc(x.name)}" loading="lazy"><strong>${esc(x.name)}</strong><p>${esc(compareDate(x.captured))}${!x.time_confirmed?'（時間未確認）':''}</p><button data-sim-photo="${x.id}" data-pair="${p.id}">照片屬性／異常處理</button></div>`).join('')}</div><p>dHash 距離 ${p.distances.dhash}／64 · aHash 距離 ${p.distances.ahash}／64 · 拍攝時間差 ${p.gap_hours===null?'未知':p.gap_hours.toFixed(2)+' 小時'}</p><p><span class="badge">${similarityReviews[p.review]}</span>${p.stale?'<span class="badge warn">資料已變動，請重新分析</span>':''}</p><button data-sim-compare="${p.id}">開啟兩張照片比對</button><label for="sim-note-${p.id}">檢閱／人工異常說明</label><textarea id="sim-note-${p.id}" maxlength="2000" placeholder="建立人工異常時必填" ${p.stale?'disabled':''}></textarea><div class="row">${[['clear','已檢閱，無須處理'],['unreviewed','改為未檢閱'],['anomaly','兩張標註人工異常']].map(([v,l])=>`<button data-sim-review="${v}" data-pair="${p.id}" data-events="${p.events.length}" ${p.stale||p.review==='anomaly'||(v==='anomaly'&&!p.a.can_write)?'disabled':''}>${l}</button>`).join('')}</div>${p.review==='anomaly'?'<p class="muted">已建立的異常請開啟照片屬性，由主管確認、排除或重開。</p>':''}<details><summary>檢閱紀錄（${p.events.length}）</summary>${p.events.map(e=>`<p>${esc(e.actor)} · ${esc(compareDate(e.at))} · ${esc(similarityReviews[e.review])}<br>${esc(e.note)}</p>`).join('')||'<p class="muted">尚未檢閱</p>'}</details></article>`;
 }
 action(async()=>{await history();if(s.run)await results(s.run)});
}
