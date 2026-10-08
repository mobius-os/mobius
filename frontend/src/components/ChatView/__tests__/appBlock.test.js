import test from 'node:test'
import assert from 'node:assert/strict'
import { appBlockFromToken, inlineBlockState, inlineBlockStateUpdate, inlineSessionRetained, inlineBlockDocumentReset } from '../markdown/appBlock.js'

const token = value => ({ type:'code', lang:'mobius-app', text:JSON.stringify(value) })
test('document reset invalidates frozen confirmation without releasing uncertain publication ownership', () => {
  assert.equal(inlineBlockDocumentReset(null), null)
  assert.equal(inlineBlockDocumentReset({ actions: [], retain: false }), null)
  const state = { retain: true, ackNonce: 'sent', actions: [
    { confirming: true, confirmation: [{ title: 'Old document', facts: [] }], disabled: false },
    { busy: true, label: 'Contributing' },
  ] }
  const reset = inlineBlockDocumentReset(state)
  assert.equal(reset.retain, true)
  assert.equal(reset.ackNonce, null)
  assert.equal(reset.actions[0].confirming, false)
  assert.equal(reset.actions[0].confirmation, null)
  assert.equal(reset.actions[0].disabled, true)
  assert.equal(reset.actions[1].disabled, true)
  assert.equal(reset.recoveryPending, true)
  assert.equal(state.actions[0].confirming, true)
})
test('checkpoint handover holds a reset owner until exact observational acknowledgement', () => {
  const keys = new Set(['send:x'])
  const parse = fields => inlineBlockState({ type: 'moebius:app-block-state', sessionId: 's',
    actions: [{ key: 'send:x', label: 'Send', disabled: false }], ...fields }, 's', keys)
  const old = inlineBlockStateUpdate(null, parse({ retain: true,
    checkpoint: { id: 'attempt-1', data: '{"phase":"uncertain"}', ignored: 'authority' } }))
  assert.deepEqual(old.checkpoint, { id: 'attempt-1', data: '{"phase":"uncertain"}' })
  const reset = inlineBlockDocumentReset(old)
  assert.equal(reset.recoveryPending, true)
  const cold = inlineBlockStateUpdate(reset, parse({ retain: false }))
  assert.equal(cold.checkpoint.id, 'attempt-1')
  assert.equal(cold.actions[0].disabled, true)
  assert.equal(inlineSessionRetained(cold, null), true)
  const wrong = inlineBlockStateUpdate(cold, parse({ checkpointAck: 'other' }))
  assert.equal(wrong.actions[0].disabled, true)
  const imported = inlineBlockStateUpdate(wrong, parse({ checkpointAck: 'attempt-1',
    checkpoint: { id: 'attempt-2', data: 'new' }, retain: false }))
  assert.equal(imported.recoveryPending, false)
  assert.equal(imported.retain, true)
  assert.equal(imported.checkpoint.id, 'attempt-2')
  const settled = inlineBlockStateUpdate(imported, parse({ checkpoint: null,
    checkpointAck: 'attempt-2', retain: false }))
  assert.equal(inlineSessionRetained(settled, null), false)
})
test('malformed or oversized replacement checkpoint fails closed without erasing prior data', () => {
  const keys = new Set(['send:x'])
  const parse = checkpoint => inlineBlockState({ type: 'moebius:app-block-state', sessionId: 's',
    actions: [{ key: 'send:x', disabled: false }], checkpoint }, 's', keys)
  const prior = inlineBlockStateUpdate(null, parse({ id: 'one', data: 'opaque' }))
  for (const bad of [{ id: '', data: 'x' }, { id: 'two', data: '💫'.repeat(9000) },
    { id: 'two', data: 7 }, []]) {
    const held = inlineBlockStateUpdate(prior, parse(bad))
    assert.deepEqual(held.checkpoint, prior.checkpoint)
    assert.equal(held.retain, true)
    assert.equal(held.actions[0].disabled, true)
    assert.match(held.recoveryError, /Open the app/)
  }
  assert.equal(inlineBlockStateUpdate(null, parse({ id: 'x', data: 7 })).retain, true)
  assert.equal(inlineBlockDocumentReset({ retain: false, actions: [{ confirming: true }] }), null)
  assert.equal(inlineBlockDocumentReset({ retain: false, actions: [], ackNonce: null }, { event: 'confirm', nonce: 'confirm' }).retain, true)
})
test('legacy same-document retain transitions still release without a checkpoint', () => {
  const keys = new Set(['x:1'])
  const parse = retain => inlineBlockState({ type: 'moebius:app-block-state', sessionId: 's',
    actions: [{ key: 'x:1', disabled: !retain }], retain }, 's', keys)
  const busy = inlineBlockStateUpdate(null, parse(true))
  assert.equal(inlineSessionRetained(busy, null), true)
  const done = inlineBlockStateUpdate(busy, parse(false))
  assert.equal(inlineSessionRetained(done, null), false)
  assert.equal(done.recoveryPending, false)
})
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

test('confirmation presents every current frozen identity without silently shortening or dropping members', () => {
  const confirmation = [{ title: 'Current change', facts: [{ label: 'Destination', value: 'actual/repo → release' }] },
    { title: 'Additional stack member', facts: [{ label: 'Version', value: 'abc123' }] }]
  const parse = value => inlineBlockState({ type: 'moebius:app-block-state', sessionId: 's', actions: [
    { key: 'send:a', confirming: true, confirmation: value },
  ] }, 's', new Set(['send:a'])).actions[0]
  assert.deepEqual(parse(confirmation).confirmation, confirmation)
  assert.equal(parse(confirmation).disabled, false)
  for (const value of [undefined, [], [{ title: '', facts: [] }],
    [{ title: 'x'.repeat(513), facts: [] }], [...confirmation, { title: 'Hidden', facts: [{ label: 'x', value: 42 }] }],
    Array.from({ length: 257 }, () => confirmation[0])]) {
    const state = parse(value)
    assert.equal(state.confirmation, null)
    assert.equal(state.disabled, true, 'an incomplete display cannot offer Confirm')
    assert.match(state.note, /Open the app/)
  }
})


test('idle offscreen sessions release their frames, but unacknowledged and uncertain publication owners stay retained', () => {
  const state = { actions: [{ busy: false, confirming: false }], retain: false, ackNonce: 'one' }
  assert.equal(inlineSessionRetained(state, null), false)
  assert.equal(inlineSessionRetained(null, null), false)
  assert.equal(inlineSessionRetained(state, { nonce: 'two' }), true)
  assert.equal(inlineSessionRetained(state, { nonce: 'one' }), false)
  assert.equal(inlineSessionRetained({ ...state, retain: true }, null), true)
  assert.equal(inlineSessionRetained({ actions: [{ busy: true }] }, null), true)
  assert.equal(inlineSessionRetained({ actions: [{ confirming: true }] }, null), true)
  const parsed = inlineBlockState({ type: 'moebius:app-block-state', sessionId: 's', actions: [],
    retain: true, ackNonce: 'two' }, 's', new Set())
  assert.equal(parsed.retain, true)
  assert.equal(parsed.ackNonce, 'two')
  // A long scroll does not latch all previous idle sessions open.
  const sessions = Array.from({ length: 100 }, (_, i) => ({ near: i > 96, state }))
  assert.equal(sessions.filter(item => item.near || inlineSessionRetained(item.state, null)).length, 3)
})


test('reset cancels idle confirmation even when its live document requested retain', () => {
  assert.equal(inlineBlockDocumentReset({ retain: true, actions: [{ confirming: true }], ackNonce: 'activate' }, { event: 'activate', nonce: 'activate' }), null)
  assert.equal(inlineBlockDocumentReset({ retain: true, actions: [{ confirming: true }], ackNonce: 'activate' }, { event: 'confirm', nonce: 'confirm' }).retain, true)
})

test('valid reset checkpoint is not reported as an application import error', () => {
  const previous = { checkpoint: { id: 'one', data: 'opaque' }, retain: true, actions: [] }
  const reset = inlineBlockDocumentReset(previous)
  assert.equal(reset.recoveryError, null)
  const cold = inlineBlockStateUpdate(reset, inlineBlockState({ type: 'moebius:app-block-state', sessionId: 's', actions: [] }, 's', new Set()))
  assert.equal(cold.recoveryError, null)
  const invalid = inlineBlockStateUpdate(cold, inlineBlockState({ type: 'moebius:app-block-state', sessionId: 's', actions: [], recoveryError: true, checkpointAck: 'one' }, 's', new Set()))
  assert.equal(invalid.retain, true); assert.ok(invalid.recoveryError); assert.deepEqual(invalid.checkpoint, previous.checkpoint)
})
