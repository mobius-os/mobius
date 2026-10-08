// Exercise actual host handover functions and app sessions with native iframe
// reloads in hermetic Chromium. No live APIs or publication handlers are used.
// Run with INLINE_RELOAD_APP_REPO=<local trusted app checkout>,
// INLINE_RELOAD_APP_REV=<40-character immutable SHA>, and optionally
// MOBIUS_FRONTEND_NODE_MODULES=<existing exact-lock frontend dependencies>.
import { spawn, execFileSync } from 'node:child_process'
import { mkdtemp, readdir, rm, readFile } from 'node:fs/promises'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'node:http'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const frontendModules = process.env.MOBIUS_FRONTEND_NODE_MODULES || join(root, 'frontend/node_modules')
const appRevision = process.env.INLINE_RELOAD_APP_REV
const appRepository = process.env.INLINE_RELOAD_APP_REPO
const hostRevision = process.env.INLINE_RELOAD_HOST_REV
if (!/^[a-f0-9]{40}$/.test(appRevision || '') || !appRepository || !existsSync(appRepository)) {
  throw Error('Set INLINE_RELOAD_APP_REPO to a local trusted checkout and INLINE_RELOAD_APP_REV to an immutable full SHA')
}
if (hostRevision && !/^[a-f0-9]{40}$/.test(hostRevision)) throw Error('INLINE_RELOAD_HOST_REV must be an immutable full SHA')
const appRoot = mkdtempSync(join(tmpdir(), 'inline-document-reload-app-'))
process.on('exit', () => rmSync(appRoot, { recursive: true, force: true }))
execFileSync('git', ['clone', '--no-hardlinks', '--quiet', '--no-checkout', resolve(appRepository), appRoot])
execFileSync('git', ['-C', appRoot, 'checkout', '--quiet', '--detach', appRevision])

function slice(source, begin, end, includeEnd = true) {
  const first = source.indexOf(begin)
  const last = source.indexOf(end, first)
  if (first < 0 || last < 0) throw Error(`Owning source anchor moved: ${begin}`)
  return source.slice(first, last + (includeEnd ? end.length : 0))
}
const sourceAt = path => hostRevision
  ? execFileSync('git', ['show', `${hostRevision}:${path}`], { cwd: root, encoding: 'utf8' })
  : readFile(join(root, path), 'utf8')
const block = await sourceAt('frontend/src/components/ChatView/markdown/AppBlock.jsx')
const canvas = await sourceAt('frontend/src/components/AppCanvas/AppCanvas.jsx')
const envelope = slice(block, 'const retained = inlineSessionRetained(', 'const allowedKeys = useMemo(() => new Set(blockSession?.actions.map(action => action.key) || []), [blockSession])')
const capability = slice(block, 'const onBlockCapability = useCallback(', '}, [isSession, allowedKeys, dispatchBlockEvent, observeBlockCapability])')
const stateCallback = slice(block, 'const onBlockState = useCallback(', '}, [sessionId, allowedKeys])')
const actionCallback = slice(block, 'const dispatchBlockEvent = useCallback(', '}, [sessionId, rememberBlockEvent])')
const sendInit = slice(canvas, 'function sendInit(v) {', '\n  }')
const frameLoad = slice(canvas, 'function handleFrameLoad(v) {', '\n  }')
const publish = slice(canvas, 'function publishBlockCapability(v) {', '\n  }')
const mounted = slice(canvas, "if (msg.type === 'moebius:frame-mounted'", '      // frame-error:', false)
const receiveState = slice(canvas, "if (msg.type === 'moebius:app-block-state'", "      if (msg.type === 'moebius:module-request'", false)
const layout = slice(canvas, '  useLayoutEffect(() => {\n    const incoming = swap.incomingVersion', '  // Inline transcript sessions', false)
const delivery = slice(canvas, '  // Inline transcript sessions', '  // ── P1-A:', false)

const childEntry = `
import React from 'react'
import {createRoot} from 'react-dom/client'
import {InlineBlockSession} from './ui/InlineBlockSession.jsx'
const scenario = parent.__scenario
const root = createRoot(document.getElementById('root'))
let initialized = false, initial, release
const records = () => structuredClone(parent.__canonical)
window.mobius = {storage:{getWithVersion:async path => {
  const id = path.split('/').at(-1).replace(/\\.json$/, '')
  parent.__reads.push(id)
  return {value:records().find(record => record.id === id), version:parent.__ledgerVersion}
}}}
const forbidden = name => { throw Error('Forbidden transport: '+name) }
window.fetch = () => forbidden('fetch')
for (const name of ['WebSocket','XMLHttpRequest','EventSource']) window[name] = class {constructor(){forbidden(name)}}
const open = (record, number) => ({...record,status:'open',number,url:'https://github.com/team/repo/pull/'+number,updated_at:'2026-01-02T00:00:00Z'})
function save(record) {
  parent.__canonical = parent.__canonical.map(previous => previous.id === record.id ? record : previous)
  parent.__ledgerVersion++
  return record
}
async function send(record) {
  parent.postMessage({type:'probe-send',id:record.id}, location.origin)
  if (scenario === 'busy' || scenario === 'busy-swap') return new Promise(resolve => { release = resolve })
  if (scenario === 'partial-batch' && record.id === 'a') return {ok:true,record:save(open(record,1))}
  if (scenario === 'failed-unacked') return {error:'Publication failed; exact result still unresolved'}
  return {uncertain:true}
}
async function sendStack(records) {
  parent.postMessage({type:'probe-send',id:'stack'}, location.origin)
  return {uncertain:true,records:[save(open(records[0],1))]}
}
const review = () => ({state:'ready',byId:Object.fromEntries(records().map(record => [record.id,{state:'ready'}]))})
const supported = () => !(scenario === 'unsupported' && parent.__loadsByVersion.v1 >= 2)
function Marker() { return <span ref={node => {if(node)parent.postMessage({type:'moebius:frame-mounted',appId:80,supportsAppBlocks:supported()},location.origin)}}/> }
function render() { root.render(<><Marker/>{supported() && <InlineBlockSession blockSession={initial} records={records()} ledgerReady={true} reviewStatus={review()} onSend={send} onSendStack={sendStack} onRefresh={render}/>}</>) }
addEventListener('message', event => {
  if (event.source !== parent || event.origin !== location.origin) return
  if (event.data.type === 'moebius:frame-init' && !initialized) {
    initialized = true; initial = event.data.blockSession; render()
  } else if (event.data.type === 'probe-ledger') render()
  else if (event.data.type === 'probe-release') release?.({ok:true,record:records()[0]})
  else if (event.data.type === 'probe-state') parent.postMessage(event.data.state,location.origin)
})
`

const fixture = `
import React,{useState,useRef,useCallback,useMemo,useEffect,useLayoutEffect,useReducer} from 'react'
import {createRoot} from 'react-dom/client'
import {flushSync} from 'react-dom'
import {inlineBlockState,inlineBlockStateUpdate,inlineBlockDocumentReset,inlineSessionRetained} from './frontend/src/components/ChatView/markdown/appBlock.js'
import {initSwapState,reduceSwap} from './frontend/src/lib/previewSwapState.js'
import {attributedFrameVersion} from './frontend/src/components/AppCanvas/appFrameProtocol.js'
const scenario = new URLSearchParams(location.search).get('case')
window.__scenario = scenario
const record = id => ({id,status:'prepared',type:'pr',repo:'team/repo',plan:{action:'pr',repo:'team/repo',base_sha:'a'.repeat(40),head_sha:'a'.repeat(40)},quality_review:{state:'all_clear',reviewed_head_sha:'a'.repeat(40)}})
window.__canonical = (scenario === 'partial-stack' || scenario === 'partial-batch') ? [record('a'),record('b')] : [record('a')]
if (scenario === 'partial-stack') window.__canonical = window.__canonical.map((rec,index) => ({...rec,plan:{...rec.plan,branch:'stack/s/'+rec.id,stack:{id:'s',position:index+1,total:2,base_branch:index?'stack/s/a':'main',parent_record_id:index?'a':''}}}))
window.__ledgerVersion = 1; window.__reads=[];window.__loadsByVersion={};window.__sends=0;window.__deliveries=[];window.__states=[];window.__inits=[];window.__capabilities=[]
const key = scenario === 'partial-batch' ? 'chat-send-batch:a,b' : 'chat-send:a'
const blockSpec = {action:{intent:key,label:'Contribute'},items:scenario==='partial-stack'?[{action:{intent:'chat-send:b',label:'Contribute'}}]:[]}
const wait = condition => new Promise((resolve,reject) => {
 const deadline=Date.now()+8000
 function check(){if(condition())resolve();else if(Date.now()>deadline)reject(Error('Timed out: '+JSON.stringify(window.__state)));else setTimeout(check,10)}
 check()
})
const assert = (value,message) => {if(!value)throw Error(message)}
function Host(){
 // Preserve the owning AppBlock hook order: deferred reset observes the event
 // hook before the state hook, rather than a fixture-only favorable ordering.
 const [blockEvent,setBlockEvent]=useState(null)
 const blockEventRef=useRef(null);blockEventRef.current=blockEvent
 const [sessionState,setSessionState]=useState(null)
 const [viewIntent,setViewIntent]=useState(null)
 const sessionId='s',isSession=true,passiveAllowed=true,canExpand=true,block=blockSpec
 ${envelope}
 const rememberBlockEvent=()=>{}
 ${stateCallback}
 ${actionCallback}
 const observeBlockCapability=supported=>window.__capabilities.push(supported),setLegacyMode=()=>{}
 ${capability}
 const [swap,dispatchSwap]=useReducer(reduceSwap,'v1',initSwapState)
 const framesRef=useRef(new Map()),loadedDocsRef=useRef(new Set()),blockDocumentsRef=useRef(new Map()),reportedBlockDocumentRef=useRef(null)
 const liveVersionRef=useRef(swap.liveVersion);liveVersionRef.current=swap.liveVersion
 const [blockDocumentRevision,setBlockDocumentRevision]=useState(0)
 const blockSessionRef=useRef(blockSession);blockSessionRef.current=blockSession
 const onBlockCapabilityRef=useRef(onBlockCapability);onBlockCapabilityRef.current=onBlockCapability
 const onBlockStateRef=useRef(onBlockState);onBlockStateRef.current=onBlockState
 const sentBlockEventRef=useRef(null),refCache=useRef(new Map())
 function getFrameRef(version){if(!refCache.current.has(version))refCache.current.set(version,node=>{if(node)framesRef.current.set(version,node);else{framesRef.current.delete(version);loadedDocsRef.current.delete(version);blockDocumentsRef.current.delete(version);refCache.current.delete(version)}});return refCache.current.get(version)}
 const accountLinkRef=useRef(null),capabilityHostRef=useRef({detachSource(){}}),storageHostRef=useRef({detachSource(){}}),frameVisibleRef=useRef(false),interactiveRef=useRef(false)
 const token='fixture',appId=80,appSlug='fixture',theme=null,capabilityContract=null
 const getEffectiveTheme=()=>({css:'',bg:'#fff'}),readAppFrameStorage=()=>({}),retireFrameMediaSession=()=>{},clearAccountLinkRegistration=()=>{}
 const sendOnlineStatus=()=>{},sendInsets=()=>{},sendImmersiveState=()=>{},sendShellShortcuts=()=>{},sendVisibility=()=>{},sendInteractivity=()=>{}
 function postToFrame(version,message){
  // Simulate the transport boundary losing an unacknowledged Confirm before
  // handler entry; native reload must hold uncertainty, not replay it.
  if(scenario.startsWith('unacked')&&window.__loadsByVersion[version]===1&&message.type==='moebius:app-block-action'&&message.event==='confirm')return
  // Protocol-fault cases model an old document that no longer answers idle
  // init echoes, so its synthetic ownership snapshot survives until reload.
  if(['missing','invalid','oversized'].includes(scenario)&&window.__loadsByVersion[version]===1&&message.type==='moebius:app-block-init')return
  if(message.type==='moebius:app-block-action')window.__deliveries.push({version,load:window.__loadsByVersion[version],event:message.event,nonce:message.nonce})
  framesRef.current.get(version)?.contentWindow.postMessage(message,location.origin)
 }
 ${publish}
 ${sendInit}
 ${frameLoad}
 ${layout}
 ${delivery}
 useEffect(()=>{
  const receive=e=>{
   const srcVersion=attributedFrameVersion(framesRef.current,e.source)
   if(srcVersion==null||e.origin!==location.origin)return
   let msg=e.data
   if(scenario==='failed-unacked'&&msg.type==='moebius:app-block-state')msg={...msg,ackNonce:null}
   if(scenario==='wrong-ack'&&window.__loadsByVersion.v1===2&&!window.__allowAck&&msg.type==='moebius:app-block-state')msg={...msg,checkpointAck:'wrong'}
   if(msg.type==='moebius:app-block-state')window.__states.push(msg)
   ${receiveState}
   ${mounted}
   if(msg.type==='probe-send')window.__sends++
  }
  addEventListener('message',receive);return()=>removeEventListener('message',receive)
 },[])
 window.__state=sessionState;window.__event=blockEvent;window.__swap=swap;window.__frames=framesRef.current;window.__documents=blockDocumentsRef.current
 window.__act=event=>dispatchBlockEvent(key,event)
 window.__reload=()=>framesRef.current.get(swap.liveVersion).contentWindow.location.reload()
 window.__version=()=>dispatchSwap({type:'version',version:'v2'})
 window.__resumeAck=()=>{window.__allowAck=true;postToFrame(swap.liveVersion,{type:'moebius:app-block-init',...blockSession})}
 window.__seed=state=>postToFrame(swap.liveVersion,{type:'probe-state',state:{type:'moebius:app-block-state',sessionId:'s',...state}})
 window.__settle=()=>{
  window.__canonical=window.__canonical.map((rec,index)=>({...rec,status:'open',number:index+1,url:'https://github.com/team/repo/pull/'+(index+1),updated_at:'2026-01-03T00:00:00Z'}));window.__ledgerVersion++
  for(const version of framesRef.current.keys()){postToFrame(version,{type:'probe-ledger'});postToFrame(version,{type:'probe-release'})}
 }
 const versions=[swap.liveVersion,...(swap.incomingVersion?[swap.incomingVersion]:[])].sort()
 return versions.map(version=><iframe key={version} src={'/child.html'+location.search} ref={getFrameRef(version)} onLoad={()=>{
  window.__loadsByVersion[version]=(window.__loadsByVersion[version]||0)+1
  // A pending state message can defer the reset updater until the render that
  // clears blockEventRef; exercise that real React scheduling path explicitly.
  if(scenario.startsWith('unacked')&&window.__loadsByVersion[version]===2)setSessionState(previous=>({...previous}))
  handleFrameLoad(version)
  window.__inits.push({version,envelope:structuredClone(blockSessionRef.current)})
 }}/>)
}
createRoot(document.getElementById('root')).render(<Host/>);
window.runWorkspaceChecks=async()=>{
 const checks=[]
 function check(name,value){assert(value,name);checks.push(name)}
 try{
  await wait(()=>window.__state?.actions[0]?.status==='Ready'&&window.__loadsByVersion.v1===1)
  check('initial first init has no false reset',!window.__inits[0].envelope.retain&&!window.__inits[0].envelope.recoveryError)
  if(scenario==='version'){
   window.__version();await wait(()=>window.__swap.liveVersion==='v2'&&window.__state?.actions[0]?.status==='Ready')
   check('native promotion sends a fresh idle envelope',window.__inits.find(entry=>entry.version==='v2')?.envelope.retain===false)
   check('promotion cannot replay an action',window.__sends===0)
   check('old frame lifetime is bounded',window.__frames.size===1&&window.__documents.size===1)
   return {status:'pass',scenario,checks,sends:window.__sends}
  }
  const genuine=['busy','busy-swap','unknown','partial-stack','partial-batch','failed-unacked','unsupported','wrong-ack'].includes(scenario)
  if(genuine||['idle','cancel','unacked','unacked-swap'].includes(scenario)){
   window.__act('activate');await wait(()=>window.__state?.actions[0]?.confirming)
  }
  if(genuine){
   window.__act('confirm');const expected=scenario==='partial-batch'?2:1
   await wait(()=>window.__sends===expected&&window.__state?.checkpoint)
   if(!['busy','busy-swap'].includes(scenario))await wait(()=>!window.__state?.actions[0]?.busy)
  }
  if(scenario==='cancel'){window.__act('cancel');await wait(()=>!window.__state?.actions[0]?.confirming&&!window.__state?.retain)}
  if(scenario.startsWith('unacked')){window.__act('confirm');await wait(()=>window.__event?.event==='confirm')}
  if(scenario==='unacked-swap'){window.__version();await wait(()=>window.__documents.get('v2')?.supported===true);check('unacked Confirm pins the native successor without authority transfer',window.__swap.liveVersion==='v1'&&window.__swap.incomingVersion==='v2'&&window.__event?.event==='confirm'&&window.__sends===0)}
  const action={key,label:'Contribute',busy:true}
  if(scenario==='missing'){window.__seed({actions:[action],retain:true});await wait(()=>window.__state?.actions[0]?.busy)}
  if(scenario==='invalid'){window.__seed({actions:[action],retain:true,checkpoint:{id:'invalid',data:'not-json'}});await wait(()=>window.__state?.checkpoint?.id==='invalid')}
  if(scenario==='oversized'){window.__seed({actions:[action],retain:true,checkpoint:{id:'oversized',data:'é'.repeat(16385)}});await wait(()=>window.__state?.recoveryPending&&window.__state?.recoveryError)}
  const before=structuredClone(window.__state),sendsBefore=window.__sends,oldConfirm=window.__event?.event==='confirm'?window.__event.nonce:null
  if(scenario==='partial-batch'||scenario==='partial-stack'){
   check('real partial publication has a canonical opened prefix and unresolved remainder',window.__canonical[0].status==='open'&&window.__canonical[1].status==='prepared')
   const phases=JSON.parse(before.checkpoint.data).phases
   check('actual partial checkpoint owns the exact unresolved publication phase',phases.length===1&&(scenario==='partial-batch'?phases[0].unitKey==='record:b'&&JSON.stringify(phases[0].phaseIds)===JSON.stringify(['b']):phases[0].unitKey==='stack:s'&&JSON.stringify(phases[0].phaseIds)===JSON.stringify(['a','b'])))
  }
  if(scenario==='busy-swap'){
   window.__version();await wait(()=>window.__documents.get('v2')?.supported===true)
   check('busy native successor cannot take the live document',window.__swap.liveVersion==='v1'&&window.__swap.incomingVersion==='v2')
   check('incoming observation cannot replace the live owner',window.__state.checkpoint.id===before.checkpoint.id)
   window.__settle();await wait(()=>window.__swap.liveVersion==='v2'&&!window.__state?.retain)
   check('exact settlement permits promotion without replay',window.__sends===1)
   check('settled native promotion releases the right link',window.__state.actions[0].links[0]?.url==='https://github.com/team/repo/pull/1')
   return {status:'pass',scenario,checks,sends:window.__sends}
  }
  const priorMessages=window.__states.length
  window.__reload();await wait(()=>window.__loadsByVersion.v1===2)
  const transfer=window.__inits.at(-1).envelope
  check('native reload replaced the same version document',window.__inits.at(-1).version==='v1'&&window.__documents.size===(scenario==='unacked-swap'?2:1))
  if(['idle','cancel'].includes(scenario)){
   await wait(()=>window.__states.length>priorMessages&&window.__state?.actions[0]?.status==='Ready'&&!window.__state?.actions[0]?.confirming)
   check('idle confirmation is dropped before first frame init',!transfer.retain&&!transfer.checkpoint&&!transfer.recoveryError)
   check('idle replacement remains evictable',!inlineSessionRetained(window.__state,window.__event))
   window.__act('activate');await wait(()=>window.__state?.actions[0]?.confirming)
   check('replacement offers fresh activation',window.__state.actions[0].confirming)
   window.__act('cancel');await wait(()=>!window.__state?.retain)
   check('fresh activation cannot replay Confirm',window.__sends===0)
  }else{
   check('unresolved ownership is retained before first init',transfer.retain===true)
   if(genuine){
    check('actual app-generated checkpoint crosses native reload',transfer.checkpoint?.id===before.checkpoint.id)
    if(scenario==='unsupported')await wait(()=>window.__documents.get('v1')?.supported===false)
    else if(scenario==='wrong-ack'){
     await wait(()=>window.__states.at(-1)?.checkpointAck==='wrong'&&window.__state?.recoveryPending)
     check('wrong observational acknowledgement cannot release owner',window.__state.checkpoint?.id===before.checkpoint.id&&window.__state.actions[0].disabled)
     window.__resumeAck();await wait(()=>window.__state?.checkpointAck===before.checkpoint.id&&!window.__state?.recoveryPending)
    }else await wait(()=>window.__state?.checkpointAck===before.checkpoint.id&&!window.__state?.recoveryPending)
   }else await wait(()=>window.__state?.recoveryPending&&window.__state?.actions[0]?.disabled)
   window.__act('activate');await new Promise(resolve=>setTimeout(resolve,20));window.__act('confirm');await new Promise(resolve=>setTimeout(resolve,60))
   check('no old Confirm nonce is delivered to the replacement document',!oldConfirm||!window.__deliveries.some(event=>event.load>=2&&event.nonce===oldConfirm))
   check('replacement cannot replay publication',window.__sends===sendsBefore)
   check('prepared/unsupported/invalid observation cannot release ownership',inlineSessionRetained(window.__state,null)&&window.__state.actions[0].disabled)
   if(genuine&&scenario!=='unsupported'){
    window.__settle();await wait(()=>!window.__state?.retain&&!window.__state?.checkpoint&&!window.__state?.recoveryPending)
    const links=[...new Set(window.__state.actions.flatMap(action=>action.links.map(link=>link.url)))]
    const expected=window.__canonical.map(rec=>rec.url)
    check('canonical exact settlement releases every right link',JSON.stringify(links)===JSON.stringify(expected))
    if(scenario==='partial-stack')check('stack rows keep their own PR link identities',window.__state.actions[0].links.length===1&&window.__state.actions[0].links[0].url===expected[0]&&window.__state.actions[1].links.length===1&&window.__state.actions[1].links[0].url===expected[1])
    check('settlement is observational, not another send',window.__sends===sendsBefore)
   }
  }
  return {status:'pass',scenario,checks,sends:window.__sends,transfer,checkpointAck:window.__state?.checkpointAck,reads:window.__reads}
 }catch(error){return {status:'fail',scenario,error:error.stack,state:window.__state,event:window.__event,swap:window.__swap,inits:window.__inits,sends:window.__sends,checks}}
}
`
const require = createRequire(join(frontendModules,'package.json'))
const {rolldown} = await import(pathToFileURL(require.resolve('rolldown')).href)
async function bundle(entry,base){
 const build=await rolldown({input:'virtual:fixture',platform:'browser',tsconfig:false,transform:{jsx:'react-jsx',define:{'process.env.NODE_ENV':JSON.stringify('production')}},resolve:{modules:[frontendModules,'node_modules']},plugins:[{name:'fixture',resolveId(id,importer){if(id==='virtual:fixture')return '\0fixture';if(importer==='\0fixture'&&id.startsWith('.'))return join(base,id)},load(id){if(id==='\0fixture')return {code:entry,moduleType:'jsx'}}}]})
 try{const {output}=await build.generate({format:'iife'});return output[0].code.replace(/<\/script/gi,'<\\/script')}finally{await build.close()}
}
const csp="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-src 'self'; connect-src 'none'; img-src data:"
const html=code=>'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="'+csp+'"><div id="root"></div><script>'+code+'</script>'
const childHtml=html(await bundle(childEntry,appRoot))
const hostHtml=html(await bundle(fixture,root))

async function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH
  for (const path of ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']) {
    if (existsSync(path)) return path
  }
  const directory = '/opt/agent-browser/browsers'
  if (existsSync(directory)) for (const name of (await readdir(directory)).sort().reverse()) {
    const path = join(directory, name, 'chrome')
    if (existsSync(path)) return path
  }
  throw new Error('No installed Chromium found; set CHROMIUM_PATH. This test never installs a browser.')
}

function cdp(browser) {
  let serial = 0, buffer = ''
  const pending = new Map(), events = []
  browser.stdio[4].on('data', chunk => {
    buffer += chunk.toString()
    let end
    while ((end = buffer.indexOf('\0')) !== -1) {
      const message = JSON.parse(buffer.slice(0, end)); buffer = buffer.slice(end + 1)
      if (message.id) {
        const entry = pending.get(message.id)
        if (!entry) continue
        pending.delete(message.id); clearTimeout(entry.timer)
        message.error ? entry.reject(new Error(JSON.stringify(message.error))) : entry.resolve(message.result)
      } else events.push(message)
    }
  })
  return { events, send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++serial
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timed out: ' + method)) }, 45000)
      pending.set(id, { resolve, reject, timer })
      browser.stdio[3].write(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }) + '\0')
    })
  } }
}

async function main(){
 const temporary=await mkdtemp(join(tmpdir(),'inline-document-reload-browser-'))
 let browser,server
 try{
  server=createServer((request,response)=>{response.setHeader('Content-Type','text/html');response.end(request.url.startsWith('/child.html')?childHtml:hostHtml)})
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  const origin='http://127.0.0.1:'+server.address().port
  browser=spawn(await chromiumPath(),['--headless=new','--no-sandbox','--disable-dev-shm-usage','--disable-background-networking','--disable-component-update','--disable-sync','--disable-default-apps','--disable-extensions','--no-first-run','--no-default-browser-check','--no-proxy-server','--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1','--remote-debugging-pipe','--user-data-dir='+join(temporary,'profile'),'about:blank'],{stdio:['ignore','ignore','pipe','pipe','pipe']})
  browser.stderr.resume()
  const protocol=cdp(browser),reports=[]
  const scenarios=process.env.INLINE_RELOAD_CASES?.split(',')||['idle','cancel','busy','unknown','partial-stack','partial-batch','failed-unacked','unacked','unacked-swap','missing','invalid','oversized','unsupported','wrong-ack','version','busy-swap']
  for(const viewport of [{name:'desktop',width:1280,height:900},{name:'phone',width:390,height:844}])for(const scenario of scenarios){
   const {targetId}=await protocol.send('Target.createTarget',{url:'about:blank'})
   const {sessionId}=await protocol.send('Target.attachToTarget',{targetId,flatten:true})
   await protocol.send('Runtime.enable',{},sessionId)
   await protocol.send('Network.enable',{},sessionId)
   await protocol.send('Network.setBlockedURLs',{urls:['https://*','ws://*','wss://*']},sessionId)
   await protocol.send('Emulation.setDeviceMetricsOverride',{width:viewport.width,height:viewport.height,deviceScaleFactor:1,mobile:viewport.name==='phone'},sessionId)
   await protocol.send('Page.enable',{},sessionId)
   await protocol.send('Page.navigate',{url:origin+'/?case='+scenario},sessionId)
   const evaluated=await protocol.send('Runtime.evaluate',{expression:`new Promise((resolve,reject)=>{const deadline=Date.now()+5000;function ready(){if(window.runWorkspaceChecks)window.runWorkspaceChecks().then(resolve,reject);else if(Date.now()>deadline)reject(Error('Fixture failed to mount'));else setTimeout(ready,20)}ready()})`,awaitPromise:true,returnByValue:true},sessionId)
   const report=evaluated.result?.value||{status:'fail',error:evaluated.exceptionDetails||evaluated}
   const network=protocol.events.filter(event=>event.sessionId===sessionId&&event.method==='Network.requestWillBeSent'&&!event.params.request.url.startsWith(origin)).map(event=>event.params.request.url)
   if(network.length){report.status='fail';report.unexpectedNetwork=network}
   reports.push({viewport,...report})
   await protocol.send('Target.closeTarget',{targetId})
  }
  const passed=reports.every(report=>report.status==='pass')
  console.log(JSON.stringify({status:passed?'pass':'fail',appRevision,hostRevision:hostRevision||'working-source',reports},null,2))
  if(!passed)process.exitCode=1
  await protocol.send('Browser.close')
 }finally{
  if(browser&&browser.exitCode===null&&browser.signalCode===null)await new Promise(resolve=>{const timer=setTimeout(()=>browser.kill('SIGKILL'),3000);browser.once('exit',()=>{clearTimeout(timer);resolve()})})
  if(server)await new Promise(resolve=>server.close(resolve))
  await rm(temporary,{recursive:true,force:true})
 }
}
main().catch(error=>{console.error(JSON.stringify({status:'fail',error:error.stack}));process.exitCode=1})
