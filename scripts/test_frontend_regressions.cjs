// Unit tests execute the production functions with a small fake UI, never a live browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const root = path.resolve(__dirname, '..');
const library = fs.readFileSync(path.join(root, 'static/library.js'), 'utf8');
const app = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const upload = fs.readFileSync(path.join(root, 'static/upload.js'), 'utf8');
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
function setup() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', innerHTML: '', disabled: false, scrollTop: 0,
      setAttribute(){}, removeAttribute(){}, querySelectorAll(){return [];},
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    console, Set, Map, Promise, JSON, Math, structuredClone,
    localStorage: {getItem(){return null;}, setItem(){}},
    document: {querySelector(){return null;}, querySelectorAll(){return [];}, addEventListener(){}},
    window: {addEventListener(){}},
    $: element, esc: String, toast(){}, clearTimeout(){},
    setTimeout(){return 1;}, selected: new Set(), tab: 'library', map: null,
    filter: {deps:[],datasets:[],tag:'',text:'',from:'',to:'',star:false,status:'submitted',county:'',town:'',village:''},
    selectedTags(){return [];},
    state: {photos:[]}, ds(){return {item_id:1};}, item(){return {fields:[]};},
    photoCacheUpsert(){}, api(){throw Error('Unexpected API call');},
  });
  vm.runInContext(app.slice(app.indexOf('let selectionAll='), app.indexOf('function drawCards(')), context);
  vm.runInContext(library, context);
  vm.runInContext(upload, context);
  vm.runInContext(`drawLibraryResults=()=>{};drawLibraryRegionControls=()=>{};drawLibraryTagChips=()=>{};drawLibraryFieldFilters=()=>{};`, context);
  return {context, element, run:code => vm.runInContext(code, context)};
}

test('F01: pending search cannot select an old token; same-query pagination keeps selection', async () => {
  const {context, element, run} = setup();
  run(`libraryState.mode='wall';libraryState.total=10;libraryState.selectionToken='OLD';
    libraryState.criteriaKey=currentCriteriaKey();libraryState.resultKey=currentCriteriaKey();libraryState.searchPending=false;`);
  // Install the actual button handler from renderLibrary, including its production guards.
  run(`{const ls=libraryState;${library.slice(library.indexOf(" $('#selectall').onclick="),library.indexOf(" $('#clearselection').onclick="))}}`);
  element('#selectall').onclick();
  assert.equal(run('selectionAll.token'), 'OLD');
  run(`filter.text='new';scheduleLibrarySearch();`);
  element('#selectall').onclick();
  assert.equal(run('selectionAll'), null);
  assert.equal(element('#selectall').disabled, true);
  const response = deferred(); context.api = () => response.promise;
  const pending = run('runLibrarySearch(libraryState.sequence,libraryState.criteriaKey)');
  response.resolve({photos:[],ids:[],total:3,located:0,missing_location:3,selection_token:'NEW'});
  await pending;
  element('#selectall').onclick();
  assert.equal(run('selectionAll.token'), 'NEW');
  assert.equal(run('selectionAll.total'), 3);
  context.api = async () => ({photos:[],has_next:false,next_offset:null,selection_token:'NEW-PAGE'});
  run('libraryState.hasNext=true;libraryState.nextOffset=100');
  await run('loadNextLibraryPage()');
  assert.equal(run('selectionAll.token'), 'NEW');
});

test('F02: historical predicates survive region reload failure without broadening', async () => {
  const {context, run} = setup();
  run(`filter.datasets=[1];libraryState.mode='wall';
    libraryState.filterOptions={fields:[{key:'legacy',type:'text',historical:true}]};
    libraryState.fieldDefinitionsScope=libraryFieldScopeKey();
    libraryState.filterOptionsScope=currentFilterOptionsScopeKey();
    libraryState.fields={'legacy|text':{value:'required'}};filter.county='新縣市';`);
  assert.equal(run('libraryRules()[0].value'), 'required');
  context.api = async name => {assert.equal(name,'filter_options');throw Error('offline');};
  await run('refreshLibraryFilterOptions()');
  assert.equal(run('libraryRules()[0].value'), 'required');
  let sent;
  context.api = async (name, data) => {sent = data;return {photos:[],ids:[],total:0,selection_token:'FILTERED'};};
  run('scheduleLibrarySearch()');
  await run('runLibrarySearch(libraryState.sequence,libraryState.criteriaKey)');
  assert.equal(sent.fields[0].value, 'required');
});

test('F02: unknown stored predicates block dispatch until definitions are loaded', async () => {
  const {context, run} = setup();
  run(`filter.datasets=[1];libraryState.mode='wall';libraryState.fields={'missing|text':{value:'must not disappear'}};scheduleLibrarySearch();`);
  let calls = 0;context.api = async () => {calls++;return {};};
  await run('runLibrarySearch(libraryState.sequence,libraryState.criteriaKey)');
  assert.equal(calls, 0);
  assert.equal(run('libraryState.selectionToken'), '');
});

test('F03: replacement dataset load completes even when the old response finishes later', async () => {
  const {context, element, run} = setup();
  const first = deferred(), second = deferred();
  context.api = (_name, data) => data.datasets[0] === 1 ? first.promise : second.promise;
  run('uploadWorkspace.dataset=1');
  const oldLoad = run('loadUploadDrafts(1)');
  run(`{const w=uploadWorkspace;renderUpload=()=>{};${upload.slice(upload.indexOf(" $('#upload-department').onchange="),upload.indexOf(" $('#upload-files-form').onsubmit="))}}`);
  element('#upload-dataset').onchange({target:{value:'2'}});
  assert.equal(run('uploadWorkspace.loading'), false);
  const newLoad = run('loadUploadDrafts(2)');
  first.resolve({photos:[{id:1}],total:1});await oldLoad;
  assert.equal(run('uploadWorkspace.loading'), true);
  second.resolve({photos:[{id:2}],total:1});await newLoad;
  assert.equal(run('uploadWorkspace.loading'), false);
  assert.equal(run('uploadWorkspace.rows[0].id'), 2);
});

test('F03: loading all 500 drafts keeps edits and merges each page once', async () => {
  const {context, run} = setup();
  let calls=0;
  context.api=async (_name,data)=>{
    calls++;
    const rows=Array.from({length:Math.min(200,500-data.offset)},(_,i)=>({id:data.offset+i,dataset:1,status:'draft',uploader:'member'}));
    const next=data.offset+rows.length;
    return {photos:rows,total:500,has_next:next<500,next_offset:next<500?next:null};
  };
  run(`tab='test';state.user={name:'member'};uploadWorkspace.dataset=1;uploadWorkspace.edits.set(0,{attrs:{note:'unsaved'}});`);
  await run('loadUploadDrafts(1)');await run('loadAllUploadDrafts(true)');
  assert.equal(calls,3);
  assert.equal(run('uploadWorkspace.rows.length'),500);
  assert.equal(run('uploadWorkspace.selected.size'),500);
  assert.equal(run("uploadWorkspace.edits.get(0).attrs.note"),'unsaved');
});

test('F04: previous cluster callbacks are inert while replacement request is pending or fails', async () => {
  const {context, run} = setup();
  let clearCount=0, clicked=0, markerClick;
  const bounds={getNorth:()=>26,getSouth:()=>24,getEast:()=>122,getWest:()=>121};
  context.map={getBounds:()=>bounds,getZoom:()=>11};
  context.L={divIcon:x=>x,marker:()=>({addTo(){return this;},on(_event,cb){markerClick=cb;return this;}})};
  context.openPhoto=()=>clicked++;
  context.markers={clearLayers(){clearCount++;}};
  run(`libraryMarkers=markers;libraryState.mode='map';`);
  context.api=async()=>({clusters:[{count:1,single_photo_id:1,lat:25,lon:121.5}]});
  await run('refreshMapClusters()');const oldClick=markerClick;
  assert.ok(oldClick);
  const deferredResponse=deferred();context.api=()=>deferredResponse.promise;
  const pending=run('refreshMapClusters()');
  oldClick();assert.equal(clicked,0);
  deferredResponse.reject(Error('offline'));await pending;
  oldClick();assert.equal(clicked,0);
  assert.ok(clearCount>=2);
});
