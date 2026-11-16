const fs = require('node:fs');
const path = require('node:path');
const ts = require('../web/node_modules/typescript');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'web/src/lib/sse.ts'), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const mod = {exports:{}};
new Function('module','exports',compiled)(mod,mod.exports);
// NOTE: frontend-probe-results.json is pre-fix evidence (2026-11-15).
// This script now also reports the post-fix SSE outcome; set
// YIYAN_UPDATE_PROBE=1 to overwrite the recorded evidence file.
async function probe(body) {
  const events=[];
  global.fetch=async()=>new Response(body, {status:200,headers:{'Content-Type':'text/event-stream'}});
  const outcome=await mod.exports.readSse('/audit', e=>events.push(e), new AbortController().signal);
  return {events,outcome};
}
(async()=>{
  const lf=await probe('id: 0\nevent: progress\ndata: {"pct":0.5}\n\n');
  const crlf=await probe('id: 0\r\nevent: progress\r\ndata: {"pct":0.5}\r\n\r\n');
  const eof=await probe('id: 0\nevent: progress\ndata: {"pct":0.5}\n\n');
  const appSource=fs.readFileSync(path.join(root,'web/src/App.tsx'),'utf8');
  // safeDecode 与 parseLocation 一起取；ErrorBoundary 不在切片里，但保险起见
  // 给 Component 一个桩，避免将来切片扩大时误判。
  const parser=appSource.slice(appSource.indexOf('function safeDecode('),appSource.indexOf('export default function App'));
  const parserJs=ts.transpileModule(parser,{compilerOptions:{target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.React}}).outputText;
  const parse=new Function('window','Component',parserJs+';return parseLocation();');
  let malformed;
  try {malformed=parse({location:{hash:'#plan/%'}},class{}).route;}
  catch(e){malformed=e.name;}
  const validRoute=parse({location:{hash:'#plan/20261018-121532-%E5%9F%8E%E5%B8%82'}},class{}).route;
  const traversalRoute=parse({location:{hash:'#plan/..'}},class{}).route;
  const result={
    malformed_plan_hash_error:malformed,
    valid_plan_route:validRoute,
    traversal_plan_route:traversalRoute,
    lf_event_count:lf.events.length,
    crlf_event_count:crlf.events.length,
    normal_eof_without_terminal_event:{
      resolved:true,
      terminal:eof.outcome.terminal,
      event_types:eof.events.map(x=>x.event)
    },
    note:'readSse now returns a terminal flag and normalizes CRLF; useRun checks the job snapshot after a non-terminal EOF and reconnects with the cursor.'
  };
  if (process.env.YIYAN_UPDATE_PROBE === '1') {
    fs.writeFileSync(path.join(__dirname,'frontend-probe-results.json'),JSON.stringify(result,null,2));
  }
  console.log(JSON.stringify(result,null,2));
})().catch(e=>{console.error(e);process.exitCode=1});

