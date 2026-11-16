const fs=require('node:fs');
const path=require('node:path');
const ts=require('../../web/node_modules/typescript');
const root=path.resolve(__dirname,'../..');
const results=[];
function compile(source, req) {
 const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const m={exports:{}};new Function('module','exports','require',js)(m,m.exports,req);return m.exports;
}
function variable(file,name){
 const sf=ts.createSourceFile(file,fs.readFileSync(file,'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 let text;
 function visit(n){if(ts.isVariableDeclaration(n)&&n.name.getText(sf)===name)text='const '+n.getText(sf)+';';ts.forEachChild(n,visit);}
 visit(sf);if(!text)throw new Error(name+' missing');return text;
}
const planFile=path.join(root,'web/src/pages/PlanPage.tsx');
async function saveBarrier(){
 let release;
 const delayed=new Promise(r=>release=r);
 const launched=[];const messages=[];
 const env={runId:'audit',plan:{run_id:'audit'},subLines:[],
   revisionRef:{current:0},chainRef:{current:Promise.resolve()},failedSavesRef:{current:0},
   setSubBusy:()=>{},setSubMsg:x=>messages.push(x),setMeta:()=>{},setSubLines:()=>{},setSubtitles:()=>{},
   saveSubtitles:()=>delayed,fetchSubtitles:async()=>({lines:[]}),
   setError:()=>{},setNotice:()=>{},useCallback:f=>f};
 const names=Object.keys(env).join(',');
 const body='const {'+names+'}=env;'+['persistSubtitles','flushSaves','startAction'].map(n=>variable(planFile,n)).join('\n')+'return {persistSubtitles,flushSaves,startAction};';
 const js=ts.transpileModule(body,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 const fn=new Function('env',js)(env);
 let saved=false;
 const pending=fn.persistSubtitles([{start:0,end:1,text:'NEW'}]).then(()=>saved=true);
 await fn.startAction((id,opts)=>launched.push({beforeSaveCompleted:!saved,expected_revision:opts.expected_revision}));
 release({revision:1,lines:[],note:'saved'});await pending;
 results.push({probe:'subtitle_save_not_in_barrier',launches:launched,revision_after_save:env.revisionRef.current});
 let supplied;
 const env2={runId:'audit',currentPalette:'auto',paletteBusy:'',meta:{revision:0},
   revisionRef:{current:1},flushSaves:async()=>true,setPaletteMsg:()=>{},setPaletteBusy:()=>{},setMeta:()=>{},setPlan:()=>{},
   savePalette:async(id,palette,revision)=>{supplied=revision;return{revision:2,palette,note:'saved'}},errText:String};
 const b2='const {'+Object.keys(env2).join(',')+'}=env;'+variable(planFile,'changePalette')+'return changePalette;';
 const f2=new Function('env',ts.transpileModule(b2,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText)(env2);
 await f2('cool');results.push({probe:'palette_uses_stale_state_revision',current_ref:1,submitted_revision:supplied});
}
async function canceledState(){
 const states=[];const fakeReact={useState(initial){const o={value:initial};states.push(o);return[initial,v=>o.value=typeof v==='function'?v(o.value):v]},useRef:v=>({current:v}),useCallback:f=>f,useEffect:()=>{}};
 global.sessionStorage={setItem(){},getItem(){return null},removeItem(){}};
 const fakeApi={createRun:async()=>({job_id:'audit-canceled'}),eventsUrl:()=>'/audit'};
 const fakeSse={readSse:async(url,handle)=>{handle({id:0,event:'done',data:{result:{stopped:true,compose:{}}}});return{terminal:true}},sleep:async()=>{}};
 const module=compile(fs.readFileSync(path.join(root,'web/src/hooks/useRun.ts'),'utf8'),n=>{
   if(n==='react')return fakeReact;if(n.includes('/api'))return fakeApi;if(n.includes('/auth'))return{authHeaders:()=>({}),guestHeaders:()=>({})};if(n.includes('/sse'))return fakeSse;throw new Error(n);
 });
 const hook=module.useRun();await hook.start({});await Promise.resolve();
 results.push({probe:'canceled_result_frontend_state',result_stopped:true,phase:states[0].value,stage:states[2].value});
}
async function mixedReviewSummary(){
 const file=path.join(root,'web/src/pages/HistoryPage.tsx');
 const detail={shots:[{review_mode:'vlm'},{review_mode:'simulated'},{review_mode:'technical_failed'}]};
 const body='const detail=env;'+variable(file,'reviewModes')+variable(file,'reviewNote')+'return reviewNote;';
 const js=ts.transpileModule(body,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 results.push({probe:'mixed_review_summary',modes:detail.shots.map(s=>s.review_mode),summary:new Function('env',js)(detail)});
}
async function ssePositive(){
 const mod=compile(fs.readFileSync(path.join(root,'web/src/lib/sse.ts'),'utf8'),()=>{});
 const events=[];
 let headers;
 global.fetch=async(url,init)=>{headers=init.headers;return new Response('id: 4\r\nevent: done\r\ndata: {"result":{}}\r\n\r\n')};
 const out=await mod.readSse('/audit',e=>events.push(e),new AbortController().signal,{'X-Guest-Id':'audit'});
 results.push({probe:'sse_crlf_and_terminal_fixed',events:events.length,terminal:out.terminal,authHeaderPreserved:headers['X-Guest-Id']==='audit'});
}
(async()=>{
 for(const fn of [saveBarrier,canceledState,mixedReviewSummary,ssePositive])try{await fn()}catch(e){results.push({probe:fn.name,probe_error:String(e.stack)})}
 fs.writeFileSync(path.join(__dirname,'frontend-probe-results.json'),JSON.stringify(results,null,2));
 console.log(JSON.stringify(results,null,2));
})().catch(e=>{console.error(e);process.exitCode=1});

