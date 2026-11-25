const jobsUI={kind:'',state:'',offset:0,limit:20,rows:[],total:0,hasNext:false,epoch:0,pollTimer:null};
const jobKindLabels={export:'大量照片匯出',similarity:'資料集近似分析',region_recompute:'行政區批次重算',semantic_index:'內容索引維護',reconcile:'檔案對帳掃描'};
const jobStateLabels={pending:'等待中',running:'執行中',completed:'已完成',partial_failed:'部分失敗',failed:'失敗',cancelled:'已取消'};
function resetJobs(){clearTimeout(jobsUI.pollTimer);jobsUI.pollTimer=null;jobsUI.epoch++;jobsUI.rows=[];jobsUI.offset=0;jobsUI.total=0;jobsUI.hasNext=false;jobsUI.kind='';jobsUI.state=''}
function renderJobs(){
 const j=jobsUI,epoch=++j.epoch;clearTimeout(j.pollTimer);j.pollTimer=null;
 const canAdmin=state.user.admin,managedDeps=new Set(state.user.managed||[]);
 const recomputableDatasets=state.datasets.filter(d=>canAdmin||managedDeps.has(item(d.item_id)?.department_id));
 $('#side').innerHTML=`<h3>統一背景工作中心</h3><p class="muted">長時間任務由獨立背景行程執行，關閉瀏覽器不會中斷。執行與下載結果均即時驗權。</p><label for="job-filter-kind">工作類型</label><select id="job-filter-kind"><option value="">全部類型</option>${Object.entries(jobKindLabels).map(([k,v])=>`<option value="${k}" ${j.kind===k?'selected':''}>${v}</option>`).join('')}</select><label for="job-filter-state">工作狀態</label><select id="job-filter-state"><option value="">全部狀態</option>${Object.entries(jobStateLabels).map(([k,v])=>`<option value="${k}" ${j.state===k?'selected':''}>${v}</option>`).join('')}</select><div style="margin-top:16px"><button id="jobs-refresh" class="primary" style="width:100%">重新整理進度</button></div>`;
 $('#main').innerHTML=`<div class="toolbar"><h1>統一背景工作中心</h1><span class="muted" id="jobs-summary">載入中…</span></div>
 ${recomputableDatasets.length||canAdmin?`<section class="card"><h3>排程背景維護工作</h3><div class="ops-grid">
  ${recomputableDatasets.length?`<div class="card"><h4>行政區批次重算</h4><label for="job-region-ds">指定資料集</label><select id="job-region-ds">${canAdmin?'<option value="">全部資料集</option>':''}${recomputableDatasets.map(d=>`<option value="${d.id}">${esc(dep(item(d.item_id)?.department_id)?.name)}／${esc(d.name)}</option>`).join('')}</select><label class="check"><input type="checkbox" id="job-region-only-failed">僅重算有座標但尚未反查成功的照片</label><button id="job-start-region" class="primary" style="margin-top:10px">送出行政區重算工作</button></div>`:''}
  ${canAdmin?`<div class="card"><h4>離線內容／OCR 索引維護</h4><label for="job-index-target">索引對象</label><select id="job-index-target"><option value="all">語意向量＋OCR 文字</option><option value="semantic">僅 AI 語意向量</option><option value="ocr">僅 OCR 文字識別</option></select><label for="job-index-mode">執行模式</label><select id="job-index-mode"><option value="build">補建缺漏（build）</option><option value="repair">重試失敗（repair）</option><option value="rebuild">清除重建（rebuild）</option></select><button id="job-start-index" class="primary" style="margin-top:10px">送出內容索引工作</button></div>`:''}
 </div></section>`:''}
 <div id="jobs-list"></div>
 <div class="row" id="jobs-pager" style="margin-top:12px" hidden><button id="jobs-prev">上一頁</button><span id="jobs-page-info" class="muted" style="text-align:center"></span><button id="jobs-next">下一頁</button></div>`;
 $('#job-filter-kind').onchange=e=>{j.kind=e.target.value;j.offset=0;loadJobs()};
 $('#job-filter-state').onchange=e=>{j.state=e.target.value;j.offset=0;loadJobs()};
 $('#jobs-refresh').onclick=()=>action(loadJobs);
 if($('#job-start-region'))$('#job-start-region').onclick=()=>action(async()=>{
  const dsVal=$('#job-region-ds').value;
  await api('jobs/create',{kind:'region_recompute',dataset:dsVal?+dsVal:null,only_failed:$('#job-region-only-failed').checked});
  toast('已排程行政區批次重算工作');await loadJobs();
 });
 if($('#job-start-index'))$('#job-start-index').onclick=()=>action(async()=>{
  await api('jobs/create',{kind:'semantic_index',target:$('#job-index-target').value,mode:$('#job-index-mode').value});
  toast('已排程內容索引維護工作');await loadJobs();
 });
 $('#jobs-prev').onclick=()=>{j.offset=Math.max(0,j.offset-j.limit);loadJobs()};
 $('#jobs-next').onclick=()=>{if(j.hasNext){j.offset+=j.limit;loadJobs()}};
 async function loadJobs(){
  if(epoch!==j.epoch||tab!=='jobs')return;
  const r=await api('jobs/list',{kind:j.kind,state:j.state,limit:j.limit,offset:j.offset});
  if(epoch!==j.epoch||tab!=='jobs')return;
  j.rows=r.jobs||[];j.total=r.total||0;j.hasNext=!!r.has_next;
  $('#jobs-summary').textContent=`共 ${j.total} 筆工作紀錄`;
  $('#jobs-pager').hidden=j.total<=j.limit;
  if(j.total>j.limit){$('#jobs-prev').disabled=j.offset===0;$('#jobs-next').disabled=!j.hasNext;$('#jobs-page-info').textContent=`第 ${j.offset+1}–${Math.min(j.offset+j.rows.length,j.total)} 筆／共 ${j.total} 筆`}
  $('#jobs-list').innerHTML=j.rows.length?j.rows.map(job=>{
   const pct=job.total?Math.min(100,Math.round((job.completed_count+job.failed_count)*100/job.total)):(job.state==='completed'?100:0);
   const active=['pending','running'].includes(job.state);
   const retryable=['failed','partial_failed','cancelled'].includes(job.state);
   const dlUrl=job.kind==='export'&&job.result_data?.url?job.result_data.url:'';
   const simRunId=job.kind==='similarity'&&job.result_data?.similarity_run_id?job.result_data.similarity_run_id:null;
   return `<article class="card job-card"><div class="toolbar"><div><strong>${esc(jobKindLabels[job.kind]||job.kind)}</strong>${job.dataset_name?` · ${esc(job.dataset_name)}`:''}<br><small class="muted">ID ${esc(job.id.slice(0,8))} · ${esc(job.actor)} · 演算法 ${esc(job.algorithm||'-')}</small></div><span class="badge ${job.state==='failed'||job.state==='partial_failed'?'warn':''}">${esc(jobStateLabels[job.state]||job.state)}${job.cancel_requested&&active?'（取消中）':''}</span></div>
   <div class="job-progress"><div class="job-progress-bar" style="width:${pct}%"></div></div>
   <p class="muted">進度：已完成 ${job.completed_count}／${job.total}（失敗 ${job.failed_count}） · 建立於 ${esc(displayCapture(job.created))}${job.finished_at?` · 結束於 ${esc(displayCapture(job.finished_at))}`:''}</p>
   ${job.error?`<p class="metadata-warning">${esc(job.error)}</p>`:''}
   <div class="row" style="margin-top:8px">${dlUrl?`<a href="${esc(dlUrl)}" class="button-link" download>下載匯出 ZIP（需原權限）</a>`:''}${simRunId?`<button type="button" data-job-open-sim="${simRunId}" data-job-ds="${job.dataset}">檢視近似分析結果 #${simRunId}</button>`:''}${active?`<button type="button" class="danger" data-job-cancel="${esc(job.id)}" ${job.cancel_requested?'disabled':''}>取消工作</button>`:''}${retryable?`<button type="button" data-job-retry="${esc(job.id)}">安全重試</button>`:''}</div></article>`;
  }).join(''):'<div class="empty">目前沒有背景工作紀錄。大量匯出、手動近似分析或資料重算工作會顯示於此。</div>';
  document.querySelectorAll('[data-job-cancel]').forEach(b=>b.onclick=()=>action(async()=>{await api('jobs/cancel',{job:b.dataset.jobCancel});toast('已送出取消請求');await loadJobs()}));
  document.querySelectorAll('[data-job-retry]').forEach(b=>b.onclick=()=>action(async()=>{await api('jobs/retry',{job:b.dataset.jobRetry});toast('已重新排入工作佇列');await loadJobs()}));
  document.querySelectorAll('[data-job-open-sim]').forEach(b=>b.onclick=()=>{similarityUI.dataset=+b.dataset.jobDs;similarityUI.run=+b.dataset.jobOpenSim;similarityUI.offset=0;tab='similarity';render()});
  if(j.rows.some(x=>['pending','running'].includes(x.state))){j.pollTimer=setTimeout(()=>action(loadJobs),2500)}
 }
 action(loadJobs);
}
