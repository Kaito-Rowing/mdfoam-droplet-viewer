/* Shared application operations are also available through the JSON CLI and MCP. */
const $ = (id) => document.getElementById(id);
const state = {schema:null, settings:{}, cases:[], results:{}, info:null, caseInfo:null, fit:null, frame:null, comparison:null};
let lang = localStorage.getItem('mdfd:lang') || 'en';
const words = {
  en: {data:'Data', local:'Local folder', scan:'Scan', settings:'Analysis settings', analyze:'Analyze', stop:'Stop', overview:'Overview', visualization:'Visualization', models:'Models & fit', saved:'Saved analyses', compare:'Compare selected', save:'Save snapshot', open:'Open', preset:'Apply example water preset', generic:'Reset to generic', advanced:'All settings (JSON, SI)', inspect:'System information', validate:'Validate', download:'Download PNG', expression:'Expression', parameters:'Parameters (JSON: initial, lower, upper, unit)', fit:'Fit selected case', evaluate:'Evaluate selected case', register:'Register / update model', loadModel:'Load model', notes:'Inspect candidates, choose fields and density thresholds, then analyze. Inputs use SI units.', empty:'No analysis yet.', ai:'AI ready · JSON CLI + MCP', modelNote:'Use x and named parameters, e.g. a*exp(-k*x). For complex models install a Python extension.', progress:'Ready', language:'日本語', conditions:'Conditions', log:'Job log'},
  ja: {data:'データ', local:'ローカルフォルダ', scan:'読込', settings:'解析条件', analyze:'解析開始', stop:'停止', overview:'一覧・比較', visualization:'可視化', models:'モデル・フィット', saved:'保存した解析', compare:'選択した結果を比較', save:'解析を保存', open:'開く', preset:'水の例のプリセットを適用', generic:'汎用設定に戻す', advanced:'全設定（JSON・SI単位）', inspect:'系の情報', validate:'設定を検証', download:'PNG保存', expression:'数式', parameters:'パラメータ（JSON: initial, lower, upper, unit）', fit:'選択ケースにフィット', evaluate:'選択ケースで評価', register:'モデルを登録・更新', loadModel:'モデル読込', notes:'候補を確認し、フィールドと密度しきい値を選んで解析してください。入力はSI単位です。', empty:'解析結果はまだありません。', ai:'AI操作対応 · JSON CLI + MCP', modelNote:'xとパラメータ名を使います。例: a*exp(-k*x)。複雑なモデルはPython拡張で追加できます。', progress:'待機中', language:'English', conditions:'条件', log:'処理ログ'}
};
function translate(){
  document.documentElement.lang=lang;
  document.querySelectorAll('[data-word]').forEach(e=>e.textContent=words[lang][e.dataset.word]||e.dataset.word);
  $('language').textContent=words[lang].language;
}
function error(e){ $('message').textContent=e.message||String(e); $('message').hidden=false; }
function notify(text){$('message').textContent=text;$('message').hidden=false;}
async function raw(path,body,blob=false){
  const res=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!res.ok){let msg=await res.text();try{msg=JSON.parse(msg).detail||msg;}catch{}throw Error(msg);}
  return blob?res.blob():res.json();
}
async function op(name,body={}){const response=await raw('/api/v1/'+name,body);if(!response.ok)throw Error(response.error.reason);return response.result;}
function option(value,label=value){const e=document.createElement('option');e.value=value;e.textContent=label;return e;}
function fillSelect(id,values,current,blank=false){const e=$(id);e.replaceChildren();if(blank)e.append(option('','— select —'));for(const v of values)e.append(option(typeof v==='string'?v:v.value,typeof v==='string'?v:v.label));e.value=values.some(v=>(typeof v==='string'?v:v.value)===current)?current:(blank?'':e.options[0]?.value||'');}
function download(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);}
function handle(id,fn){$(id).onclick=()=>Promise.resolve().then(fn).catch(error);}
function settings(){
  const result=JSON.parse($('advanced-settings').value);
  for(const key of ['density_field','number_density_field','temperature_field','species','substrate_normal'])result[key]=$(key).value;
  for(const key of ['liquid_min','gas_min','gas_max','molecule_mass','substrate_position'])result[key]=$(key).value.trim()===''?(key==='molecule_mass'?0:null):Number($(key).value);
  result.region_zones=[...$('region_zones').selectedOptions].map(o=>o.value);
  result.droplet_zones=[...$('droplet_zones').selectedOptions].map(o=>o.value);
  result.temperature_enabled=$('temperature_enabled').checked;result.pressure_enabled=$('pressure_enabled').checked;
  state.settings=result;return result;
}
function renderSettings(){
  const s=state.settings, fields=(state.caseInfo?.fields||[]).map(f=>f.name), candidates=state.caseInfo?.candidates;
  for(const key of ['density_field','number_density_field','temperature_field']){
    const values=[...new Set([...fields,...(s[key]?[s[key]]:[])])];
    fillSelect(key,values,s[key],true);
  }
  const species=[...new Set([...(candidates?.species||[]),...(s.species?[s.species]:[])])];
  fillSelect('species',species,s.species,true);
  for(const key of ['liquid_min','gas_min','gas_max','molecule_mass','substrate_position'])$(key).value=s[key]??'';
  $('substrate_normal').value=s.substrate_normal||'+z';
  for(const key of ['temperature_enabled','pressure_enabled'])$(key).checked=s[key];
  const zones=Object.keys(state.caseInfo?.mesh?.zones||{});
  for(const key of ['region_zones','droplet_zones']){
    $(key).replaceChildren(...zones.map(z=>option(z)));for(const opt of $(key).options)opt.selected=(s[key]||[]).includes(opt.value);
  }
  $('advanced-settings').value=JSON.stringify(s,null,2);
}
async function inspectCase(){
  if(!state.cases.length)return;
  state.caseInfo=await op('inspect',{case:$('case').value||state.cases[0].name});
  $('system').textContent=JSON.stringify(state.caseInfo,null,2);
  const c=state.caseInfo.candidates;
  // A single candidate can be selected; multiple candidates remain an explicit user choice.
  if(!state.settings.density_field&&c.density.length===1)state.settings.density_field=c.density[0];
  if(!state.settings.temperature_field&&c.temperature.length===1)state.settings.temperature_field=c.temperature[0];
  if(!state.settings.species&&c.species.length===1)state.settings.species=c.species[0];
  renderSettings();
  const times=state.caseInfo.times||[];$('time').max=Math.max(0,times.length-1);$('time').value=0;
}
async function scan(){
  const result=await op($('source').value==='local'?'discover':'remote_discover',{path:$('path').value,subpath:$('subpath').value||'auto'});
  state.cases=result.cases;state.results={};state.fit=null;state.comparison=null;
  fillSelect('case',state.cases.map(c=>c.name),'');await inspectCase();renderOverview();
}
function label(q){return state.schema.quantities[q]?.label||q;}
function plot(id,traces,xlabel,ylabel){Plotly.react(id,traces,{paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{color:'#dce6ec',family:'system-ui'},margin:{l:75,r:25,t:25,b:65},xaxis:{title:{text:xlabel},gridcolor:'#263c48',zerolinecolor:'#39515d'},yaxis:{title:{text:ylabel},gridcolor:'#263c48',zerolinecolor:'#39515d'},legend:{orientation:'h',y:-.25},colorway:['#64dac0','#e9b663','#77a8ed','#e28d9b','#b99cf1']},{responsive:true,displaylogo:false,displayModeBar:false});}
function rowsOf(name){return(state.results[name]?.rows||[]).filter(r=>r.status==='ok');}
function format(x){return typeof x==='number'?Number(x.toPrecision(5)).toString():x??'—';}
function table(rows){
  const table=document.createElement('table');if(!rows.length)return table;
  const keys=Object.keys(rows[0]),head=document.createElement('tr');for(const key of keys){const th=document.createElement('th');th.textContent=key;head.append(th);}table.append(head);
  for(const row of rows){const tr=document.createElement('tr');for(const key of keys){const td=document.createElement('td');td.textContent=format(row[key]);tr.append(td);}table.append(tr);}return table;
}
function renderOverview(){
  const q=$('quantity').value||'volume';let series=[];
  for(const [name,result]of Object.entries(state.results)){const rows=(result.rows||[]).filter(r=>r.status==='ok');series.push({name,x:rows.map(r=>r.time),y:rows.map(r=>r[q]),mode:'lines+markers',marker:{size:4}});}
  if(state.comparison){series=[];for(const item of state.comparison.items){for(const[name,c]of Object.entries(item.cases||{})){const rows=c.rows||[];series.push({name:item.name+' / '+name,x:rows.map(r=>r.time),y:rows.map(r=>r[q]),mode:'lines'});}}}
  plot('overview-plot',series,'time [s]',label(q)+' [SI]');
  $('summary-table').replaceChildren(table(Object.entries(state.results).map(([name,r])=>({case:name,status:r.status,rows:r.rows?.length||0,...r.summary}))));
  const rows=rowsOf($('case').value);$('rows-table').replaceChildren(table(rows.slice(0,500)));
  $('count').textContent=Object.keys(state.results).length+' cases';
}
async function refreshResults(){const r=await op('results');state.results=r.results;renderOverview();await refreshModels();}
async function analyze(){
  const s=settings();const check=await op('validate_settings',{settings:s});if(check.errors.length)throw Error(check.errors.join('\n'));
  state.comparison=null;await op('analyze',{settings:s});notify('Analysis started');
}
async function poll(){
  try{const j=await op('job_status');$('progress').textContent=`${j.state} · ${j.done||0}/${j.total||0}`;$('logs').textContent=(j.logs||[]).map(l=>`${l.t} ${l.level}: ${l.msg}`).join('\n');
    $('analyze').disabled=['running','stopping'].includes(j.state);$('stop').disabled=!['running','stopping'].includes(j.state);
    if(state.lastJob!==j.state&&['done','stopped','error'].includes(j.state)){await refreshResults();if(state.cases.length)await inspectCase();}
    state.lastJob=j.state;
  }catch(e){error(e);}finally{setTimeout(poll,1000);}
}
function vizRequest(){
  const opts={grid:true,molecules:$('molecules').checked,show_molecules:$('molecules').checked,view:$('viz-mode').value==='3d'?'3d':'2d',projection:$('plane').value,max_points:20000,slice_color:'rho'};
  if($('viz-mode').value==='slice'&&state.frame?.grid){const plane=$('plane').value,k=3-'xyz'.indexOf(plane[0])-'xyz'.indexOf(plane[1]);const layers=[...new Set(state.frame.grid.cc.map(p=>p[k]))].sort((a,b)=>a-b);opts.slice_pos=layers[Math.min(+$('slice').value,layers.length-1)];}
  return{case:$('case').value,time:state.caseInfo?.times?.[+$('time').value]?.name,settings:settings(),opts};
}
async function showFrame(){
  const request=vizRequest();if(!request.time)throw Error('No timestep available');
  const fr=await raw('/api/viz',request);state.frame=fr;$('time-label').textContent=fr.time_name+' s';
  $('contact').textContent=JSON.stringify({coordinate_frame:fr.coordinate_frame,contact:fr.contact},null,2);
  const mode=$('viz-mode').value,plane=$('plane').value,axes={x:0,y:1,z:2};let traces=[];
  if(mode==='slice'){
    const cc=fr.grid.cc,i=axes[plane[0]],j=axes[plane[1]],k=3-i-j;
    const layers=[...new Set(cc.map(p=>p[k]))].sort((a,b)=>a-b);$('slice').max=Math.max(0,layers.length-1);
    const pos=layers[Math.min(+$('slice').value,layers.length-1)];
    const selected=cc.map((p,index)=>({p,index})).filter(o=>o.p[k]===pos);
    traces=[{type:'scatter',mode:'markers',x:selected.map(o=>o.p[i]),y:selected.map(o=>o.p[j]),marker:{symbol:'square',size:10,color:selected.map(o=>fr.grid.rho[o.index]),colorscale:'Viridis',showscale:true,colorbar:{title:{text:'density'}}}}];
  }else{
    const scatter=(points,name,color)=>mode==='3d'?{type:'scatter3d',mode:'markers',name,x:points.map(p=>p[0]),y:points.map(p=>p[1]),z:points.map(p=>p[2]),marker:{size:2,color}}:{type:'scatter',mode:'markers',name,x:points.map(p=>p[axes[plane[0]]]),y:points.map(p=>p[axes[plane[1]]]),marker:{size:4,color}};
    traces.push(scatter(fr.iso,'isodensity','#64dac0'));
    if(mode==='3d'){
      traces.push(scatter(fr.cells.liquid,'liquid cells','#77a8ed'));
      if(fr.molecules?.pos)traces.push(scatter(fr.molecules.pos,'molecules','#e9b663'));
    }else if(fr.contact.ok){
      const c=fr.contact,center=[c.xc,c.yc,c.zc],a=axes[plane[0]],b=axes[plane[1]],angles=Array.from({length:181},(_,i)=>i*Math.PI/90);
      traces.push({mode:'lines',name:'sphere fit',x:angles.map(t=>center[a]+c.R*Math.cos(t)),y:angles.map(t=>center[b]+c.R*Math.sin(t)),line:{color:'#e9b663'}});
      if(b===2)traces.push({mode:'lines',name:'substrate',x:[fr.bounds_min[a],fr.bounds_max[a]],y:[c.z_base,c.z_base],line:{dash:'dash',color:'#e28d9b'}});
    }
  }
  plot('viz-plot',traces,'substrate '+plane[0]+' [m]','substrate '+plane[1]+' [m]');
  Plotly.relayout('viz-plot',mode==='3d'?{scene:{aspectmode:'data',xaxis:{title:{text:'u [m]'}},yaxis:{title:{text:'v [m]'}},zaxis:{title:{text:'w [m]'}}}}:{'yaxis.scaleanchor':'x'});
}
function modelDefinition(){return{id:$('model-id').value,label:$('model-label').value,kind:'expression',expression:$('expression').value,x_quantity:$('model-x').value,y_quantity:$('model-y').value,x_unit:$('x-unit').value,y_unit:$('y-unit').value,parameters:JSON.parse($('parameters').value)};}
async function refreshModels(){const r=await op('list_models');state.modelDefinitions=r.models;fillSelect('registered-models',r.models.map(m=>({value:m.id,label:m.label||m.id})),$('registered-models').value,true);if(r.plugin_errors.length)notify(JSON.stringify(r.plugin_errors));}
function loadModel(){const m=state.modelDefinitions.find(m=>m.id===$('registered-models').value);if(!m)return;for(const[id,value]of Object.entries({'model-id':m.id,'model-label':m.label,expression:m.expression,'model-x':m.x_quantity,'model-y':m.y_quantity,'x-unit':m.x_unit,'y-unit':m.y_unit}))$(id).value=value;$('parameters').value=JSON.stringify(m.parameters,null,2);$('expression').disabled=m.kind==='python';}
async function registerModel(){const result=await op('register_model',{definition:modelDefinition(),overwrite:true});await refreshModels();$('registered-models').value=result.id;notify('Model registered');}
async function modelCurve(fit){
  const id=$('registered-models').value||$('model-id').value;
  const m=state.modelDefinitions.find(m=>m.id===id);if(!m)throw Error('Register or select a model first');
  const rows=rowsOf($('case').value),x=rows.map(r=>r[m.x_quantity]),y=rows.map(r=>r[m.y_quantity]);
  let result;
  if(fit){const window={};if($('window-min').value!=='')window.min=Number($('window-min').value);if($('window-max').value!=='')window.max=Number($('window-max').value);
    result=await op('fit_model',{model_id:id,case:$('case').value,window});state.fit=result;$('fit-result').textContent=JSON.stringify(result,null,2);
    if(result.status!=='converged')throw Error(result.reason);
  }else{const pairs=x.map((v,i)=>({x:v,y:y[i]})).filter(o=>Number.isFinite(o.x)&&Number.isFinite(o.y));result=await op('evaluate_model',{model_id:id,x:pairs.map(o=>o.x)});result.observed=pairs.map(o=>o.y);result.predicted=result.y;}
  plot('model-plot',[{name:'observed',mode:'markers',x:result.x,y:result.observed},{name:m.label||id,mode:'lines',x:result.x,y:result.predicted}],`${m.x_quantity} [${m.x_unit}]`,`${m.y_quantity} [${m.y_unit}]`);
}
async function savedList(){const r=await op('list_saved');const items=r.saved||r.items||[];state.saved=items;fillSelect('saved-name',items.map(i=>i.name),'');$('compare-names').replaceChildren(...items.map(i=>option(i.name)));}
async function openSaved(){const r=await op('open',{name:$('saved-name').value});state.cases=r.cases;state.comparison=null;fillSelect('case',r.cases.map(c=>c.name),'');const results=await op('results');state.results=results.results;const first=Object.values(state.results)[0];if(first){const payload=await raw('/api/info');state.info=payload;}
  // Load actual analysis settings from server results metadata via the result endpoint.
  const all=await raw('/api/results');if(all.run_meta?.settings){state.settings=all.run_meta.settings;renderSettings();}
  renderOverview();try{await inspectCase();}catch(e){notify(e.message);}const fits=Object.values(results.models);if(fits.length){drawStoredFit(fits[fits.length-1]);}
}
function drawStoredFit(fit){state.fit=fit;$('fit-result').textContent=JSON.stringify(fit,null,2);if(fit.status==='converged'){const m=fit.model;plot('model-plot',[{name:'observed',mode:'markers',x:fit.x,y:fit.observed},{name:m.label||m.id,mode:'lines',x:fit.x,y:fit.predicted}],`${m.x_quantity} [${m.x_unit}]`,`${m.y_quantity} [${m.y_unit}]`);}}
async function init(){
  state.schema=await op('schema');state.info=await raw('/api/info');state.settings=state.info.defaults;
  const qs=Object.keys(state.schema.quantities);fillSelect('quantity',qs,'volume');fillSelect('model-x',qs,'time');fillSelect('model-y',qs,'volume');
  renderSettings();translate();await refreshModels();await savedList();renderOverview();
  $('language').onclick=()=>{lang=lang==='en'?'ja':'en';localStorage.setItem('mdfd:lang',lang);translate();};
  handle('scan',scan);handle('validate',async()=>{const r=await op('validate_settings',{settings:settings()});notify(r.errors.length?r.errors.join('\n'):'Settings valid');});
  handle('analyze',analyze);handle('stop',()=>op('cancel'));handle('inspect',inspectCase);
  handle('preset',()=>{state.settings={...settings(),...state.schema.presets.water_example.settings};renderSettings();notify(state.schema.presets.water_example.note);});
  handle('generic',()=>{state.settings={...state.info.defaults};renderSettings();});
  handle('apply-advanced',()=>{state.settings=JSON.parse($('advanced-settings').value);renderSettings();});
  handle('settings-export',()=>download(new Blob([JSON.stringify(settings(),null,2)],{type:'application/json'}),'settings.json'));
  $('settings-import').onchange=async e=>{try{state.settings={...state.info.defaults,...JSON.parse(await e.target.files[0].text())};renderSettings();}catch(e){error(e);}};
  $('case').onchange=()=>inspectCase().then(renderOverview).catch(error);$('quantity').onchange=renderOverview;
  for(const id of ['time','viz-mode','plane','slice','molecules'])$(id).onchange=()=>showFrame().catch(error);
  handle('show-frame',showFrame);
  handle('viz-png',async()=>{const req=vizRequest();req.opts.view=$('viz-mode').value==='slice'?'slice':req.opts.view;download(await raw('/api/viz/png',req,true),'droplet.png');});
  handle('viz-gif',async()=>{const req=vizRequest();req.opts.view=$('viz-mode').value==='slice'?'slice':req.opts.view;download(await raw('/api/viz/gif',{...req,fps:5},true),'droplet.gif');});
  handle('overview-png',()=>downloadPlot('overview-plot','comparison.png'));
  handle('model-png',()=>downloadPlot('model-plot','model-fit.png'));
  handle('csv',async()=>download(await raw('/api/export/csv',{names:Object.keys(state.results)},true),'analysis.zip'));
  handle('register',registerModel);handle('load-model',loadModel);handle('fit',()=>modelCurve(true));handle('evaluate',()=>modelCurve(false));
  handle('save',async()=>{await op('save',{name:$('save-name').value,overwrite:$('overwrite').checked});await savedList();notify('Saved');});
  handle('open',openSaved);handle('compare',async()=>{const names=[...$('compare-names').selectedOptions].map(o=>o.value);state.comparison=await op('compare',{names});renderOverview();});
  handle('ssh-connect',async()=>{await op('remote_connect',{host:$('ssh-host').value,username:$('ssh-user').value,port:Number($('ssh-port').value),key_file:$('ssh-key').value,password:$('ssh-password').value});$('ssh-password').value='';notify('SSH connected');});
  handle('ssh-list',async()=>{$('remote-files').textContent=JSON.stringify(await op('remote_list',{path:$('path').value||'.'}),null,2);});
  $('source').onchange=()=>{$('ssh').hidden=$('source').value!=='remote';};
  handle('ssh-hosts',async()=>{$('remote-files').textContent=JSON.stringify(await op('remote_hosts'),null,2);});
  const active=await raw('/api/cases');
  if(active.cases.length){state.cases=active.cases;fillSelect('case',active.cases.map(c=>c.name),'');const r=await raw('/api/results');state.results=r.results;if(r.run_meta?.settings)state.settings=r.run_meta.settings;renderSettings();renderOverview();try{await inspectCase();}catch(e){notify(e.message);}}
  poll();
}
async function downloadPlot(id,name){
  const gd=$(id),series=(gd.data||[]).map(t=>({name:t.name,x:Array.from(t.x||[]),y:Array.from(t.y||[]),mode:t.mode||'lines'}));
  const spec={kind:'xy',series,xlabel:gd.layout.xaxis.title.text,ylabel:gd.layout.yaxis.title.text};
  download(await raw('/api/figure/png',{spec},true),name);
}
init().catch(error);
