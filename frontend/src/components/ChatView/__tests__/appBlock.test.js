import test from 'node:test'
import assert from 'node:assert/strict'
import { appBlockFromToken, inlineBlockState } from '../markdown/appBlock.js'

const token = value => ({ type:'code', lang:'mobius-app', text:JSON.stringify(value) })
test('app blocks carry a destination and snapshot, not authority', () => {
  const block = appBlockFromToken(token({app:'contribute',intent:'chat-pull:owner/repo#7',title:'PR #7',facts:[{label:'Files',value:'4'}],approved:true,height:9999}))
  assert.equal(block.height,640)
  assert.equal(block.approved,undefined)
  assert.equal(block.intent,'chat-pull:owner/repo#7')
  assert.equal(block.inline,true)
  assert.deepEqual(block.facts,[{label:'Files',value:'4'}])
  assert.match(block.href,/intent=chat-pull%3Aowner%2Frepo%237/)
})
test('link-only snapshots preserve facts and navigate without mounting an inline app', () => {
  const block = appBlockFromToken(token({app:'contribute',intent:'pull-request:owner/repo#7',title:'PR #7',inline:false,facts:[{label:'Repository',value:'owner/repo',href:'https://github.com/owner/repo'},{label:'Author',value:'octocat'}]}))
  assert.equal(block.inline,false)
  assert.deepEqual(block.facts,[{label:'Repository',value:'owner/repo',href:'https://github.com/owner/repo'},{label:'Author',value:'octocat'}])
  assert.match(block.href,/intent=pull-request%3Aowner%2Frepo%237/)
})
test('snapshot fact links admit only uncredentialed HTTPS URLs', () => {
  const block = appBlockFromToken(token({app:'example',intent:'open:item',title:'Item',facts:[
    {label:'Unsafe',value:'text',href:'javascript:alert(1)'},
    {label:'Credentials',value:'text',href:'https://user:pass@example.com/'},
  ]}))
  assert.deepEqual(block.facts,[{label:'Unsafe',value:'text'},{label:'Credentials',value:'text'}])
})
test('invalid/incomplete blocks stay ordinary code, never mount an app', () => {
  for (const value of [{app:'../auth',intent:'pr:x',title:'x'},{app:'contribute',intent:'javascript: alert(1)',title:'x'}, {app:'contribute',intent:'pr:x'},null]) assert.equal(appBlockFromToken(token(value)),null)
  assert.equal(appBlockFromToken({type:'code',lang:'mobius-app',text:'{'}),null)
  assert.equal(appBlockFromToken({type:'code',lang:'json',text:'{}'}),null)
})
test('pull-request snapshots carry a validated GitHub row and drop malformed parts', () => {
  const block = appBlockFromToken(token({app:'contribute',intent:'pull-request:owner/repo#7',title:'Fix the thing',inline:false,pull:{
    repo:'owner/repo',number:7,state:'draft',author:'octocat',files:4,additions:94,deletions:1,url:'https://github.com/owner/repo/pull/7',
    labels:[{name:'bug',color:'d73a4a'},{name:'area: ui',color:'not-a-color'},{name:''},{name:7}],
  }}))
  assert.deepEqual(block.pull,{repo:'owner/repo',repoUrl:'https://github.com/owner/repo',number:7,state:'draft',badges:[],author:'octocat',files:4,additions:94,deletions:1,
    url:'https://github.com/owner/repo/pull/7',labels:[{name:'bug',color:'d73a4a'},{name:'area: ui'}]})
  for (const pull of [{repo:'../x',number:7,state:'open'},{repo:'owner/repo',number:0,state:'open'},{repo:'owner/repo',number:7,state:'approved'}]) {
    assert.equal(appBlockFromToken(token({app:'contribute',intent:'pull-request:owner/repo#7',title:'x',pull})).pull,null)
  }
  assert.equal(appBlockFromToken(token({app:'contribute',intent:'pull-request:owner/repo#7',title:'x',pull:{repo:'owner/repo',number:7,state:'open',url:'javascript:alert(1)'}})).pull.url,undefined)
  for (const url of ['https://example.com/not-the-pr', 'https://github.com/other/repo/pull/7', 'https://github.com/owner/repo/pull/8']) {
    assert.equal(appBlockFromToken(token({app:'contribute',intent:'pull-request:owner/repo#7',title:'x',pull:{repo:'owner/repo',number:7,state:'open',url}})).pull.url,undefined)
  }
})
test('an app may name its inline expand control, bounded and never required', () => {
  const named = appBlockFromToken(token({app:'contribute',intent:'chat-prepared:rec-1',title:'Fix',expand_label:'  Review and send  '}))
  assert.equal(named.expandLabel,'Review and send')
  assert.equal(appBlockFromToken(token({app:'contribute',intent:'chat-prepared:rec-1',title:'Fix',expand_label:'x'.repeat(90)})).expandLabel.length,40)
  assert.equal(appBlockFromToken(token({app:'contribute',intent:'chat-prepared:rec-1',title:'Fix',expand_label:7})).expandLabel,'')
})

test('a proposed PR has no number yet, carries toned badges, and may name one app action', () => {
  const block = appBlockFromToken(token({app:'contribute',intent:'chat-prepared:rec-1',title:'Fix it',
    action:{label:'Send PR',intent:'chat-send:rec-1'},
    pull:{repo:'owner/repo',state:'proposed',files:3,additions:5,deletions:1,
      badges:[{label:'All clear',tone:'success'},{label:'Odd',tone:'neon'},{label:''},{label:'a'},{label:'b'}]}}))
  assert.equal(block.pull.number,null)
  assert.equal(block.pull.repoUrl,'https://github.com/owner/repo')
  assert.deepEqual(block.pull.badges,[{label:'All clear',tone:'success'},{label:'Odd',tone:'neutral'},{label:'a',tone:'neutral'}])
  assert.deepEqual(block.action,{label:'Send PR',intent:'chat-send:rec-1'})
  // Only a proposed PR may omit its number; a link-only block offers no action.
  assert.equal(appBlockFromToken(token({app:'contribute',intent:'x:1',title:'t',pull:{repo:'owner/repo',state:'open'}})).pull,null)
  assert.equal(appBlockFromToken(token({app:'contribute',intent:'x:1',title:'t',inline:false,action:{label:'Go',intent:'y:1'}})).action,null)
  assert.equal(appBlockFromToken(token({app:'contribute',intent:'x:1',title:'t',action:{label:'Go',intent:'javascript: 1'}})).action,null)
})
test('a batch block keeps up to 12 valid items, each its own destination, under one action', () => {
  const items = Array.from({ length: 14 }, (_, i) => ({ title: `Item ${i}`, intent: `review:rec-${i}`, pull: { repo: 'owner/repo', state: 'proposed' } }))
  const block = appBlockFromToken(token({ app: 'contribute', intent: 'review:batch', title: 'Ready to contribute',
    action: { label: 'Contribute all', intent: 'chat-send-batch:rec-0,rec-1' },
    items: [{ title: '', intent: 'review:x' }, { title: 'Bad', intent: 'javascript: x' }, ...items] }))
  assert.equal(block.items.length, 12)
  assert.equal(block.items[0].title, 'Item 0')
  assert.match(block.items[0].href, /intent=review%3Arec-0/)
  assert.equal(block.items[0].pull.state, 'proposed')
  assert.deepEqual(block.action, { label: 'Contribute all', intent: 'chat-send-batch:rec-0,rec-1' })
  assert.deepEqual(appBlockFromToken(token({ app: 'contribute', intent: 'x:1', title: 't' })).items, [])
})

test('inline sessions are opt-in and preserve link-only and legacy blocks', () => {
  assert.equal(appBlockFromToken(token({ app: 'contribute', intent: 'x:1', title: 'x', interaction: 'inline' })).interaction, 'inline')
  assert.equal(appBlockFromToken(token({ app: 'contribute', intent: 'x:1', title: 'x' })).interaction, null)
  assert.equal(appBlockFromToken(token({ app: 'contribute', intent: 'x:1', title: 'x', inline: false, interaction: 'inline' })).interaction, null)
})

test('all twelve batch records survive opaque app intents without losing their shared action', () => {
  for (const width of [36, 128]) {
    const ids = Array.from({ length: 12 }, (_, i) => width === 36
      ? `00000000-0000-4000-8000-${i.toString(16).padStart(12, '0')}`
      : `${i}`.padEnd(width, 'a'))
    const intent = `chat-send-batch:${ids.join(',')}`
    for (const destination of [`review:${ids[0]}`, intent]) {
      const block = appBlockFromToken(token({ app: 'contribute', intent: destination, title: 'Prepared contributions',
        action: { label: 'Contribute all', intent },
        items: ids.map(id => ({ title: id, intent: `review:${id}`,
          action: { label: 'Contribute', intent: `chat-send:${id}` } })) }))
      assert.equal(block.intent, destination)
      assert.deepEqual(block.action, { label: 'Contribute all', intent })
      assert.equal(block.items.length, 12)
      assert.equal(new URL(block.href, 'https://example.test').searchParams.get('intent'), destination)
      assert.ok(block.items.every(item => item.action))
    }
  }
})

test('the complete app block bounds opaque intents while malformed destinations remain rejected', () => {
  assert.equal(appBlockFromToken(token({ app: 'example', intent: `open:${'x'.repeat(16384)}`, title: 'Too large' })), null)
  for (const intent of ['open:', 'open:two words', 'open:line\nbreak', ':item']) {
    assert.equal(appBlockFromToken(token({ app: 'example', intent, title: 'Invalid' })), null)
    const block = appBlockFromToken(token({ app: 'example', intent: 'open:item', title: 'Valid',
      action: { label: 'Open', intent }, items: [{ title: 'Invalid', intent }] }))
    assert.equal(block.action, null)
    assert.deepEqual(block.items, [])
  }
})

test('inline state is session and key scoped, bounded, plain, and uncredentialed HTTPS only', () => {
  const keys = new Set(['x:1'])
  assert.equal(inlineBlockState({ type: 'moebius:app-block-state', sessionId: 'other', actions: [] }, 'live', keys), null)
  const state = inlineBlockState({ type: 'moebius:app-block-state', sessionId: 'live', notice: 'n'.repeat(600), actions: [
    { key: 'x:1', label: 'L'.repeat(80), note: 'N'.repeat(600), status: 'Contributing', statusTone: 'attention', links: [
      { label: 'PR', url: 'https://github.com/owner/repo/pull/1' },
      { label: 'bad', url: 'https://user:password@github.com/owner/repo/pull/2' },
      { label: 'bad', url: 'javascript:alert(1)' },
    ] },
    { key: 'unknown', label: 'Wrong' },
  ] }, 'live', keys)
  assert.equal(state.actions.length, 1)
  assert.equal(state.actions[0].label.length, 40)
  assert.equal(state.actions[0].note.length, 500)
  assert.equal(state.actions[0].status, 'Contributing')
  assert.deepEqual(state.actions[0].links, [{ label: 'PR', url: 'https://github.com/owner/repo/pull/1' }])
  assert.equal(state.notice.length, 500)
})

test('live badges may replace a saved snapshot without granting an action or markup', () => {
  const state = badges => inlineBlockState({ type: 'moebius:app-block-state', sessionId: 's',
    actions: [{ key: 'x:1', ...(badges === undefined ? {} : { badges }) }] }, 's', new Set(['x:1'])).actions[0]
  assert.equal(Object.hasOwn(state(), 'badges'), false)
  assert.deepEqual(state([]).badges, [])
  assert.deepEqual(state([{ label: 'Current', tone: 'neon' }, null]).badges, [{ label: 'Current', tone: 'neutral' }])
  assert.equal(state([]).confirming, false)
})

test('live batch summary is bounded plain text and cannot alter saved item titles', () => {
  const message = { type: 'moebius:app-block-state', sessionId: 's', actions: [], summary: '  ' + 'x'.repeat(300) + '  ' }
  assert.equal(inlineBlockState(message, 's', new Set()).summary.length, 240)
  assert.equal(inlineBlockState({ ...message, summary: 42 }, 's', new Set()).summary, '')
})
