/** Browser-only steering fixture: delayed sibling edit versus committed cut. */
export async function installSteerQueueFixture(page, chat) {
  await page.addInitScript(({ chat }) => {
    const nativeFetch = window.fetch.bind(window)
    const target = `/api/chats/${chat.id}`
    const steered = { role: 'user', cid: 'steer-repro-a',
      content: 'Please check the earlier answers too.', ts: Date.now() - 5000 }
    const sibling = { role: 'user', cid: 'steer-repro-b',
      content: 'Keep this follow-up queued.', ts: Date.now() - 4000 }
    const initial = { role: 'user', cid: 'steer-repro-start',
      content: 'Review the footer behavior.', ts: Date.now() - 10000 }
    const before = { role: 'assistant', id: 'steer-repro-a1',
      content: 'I am checking the footer behavior.', ts: Date.now() - 9000,
      blocks: [{ type: 'text', content: 'I am checking the footer behavior.' }] }
    const state = window.__steerFixture = {
      pending: [steered, sibling], messages: [initial, before],
      patchRequested: false, steerRequested: false,
    }
    const runtime = () => ({
      running: true, run_id: 'steer-repro-run', run_status: 'running',
      runtime_revision: 1000000000, pending_messages: state.pending,
      pending_question_id: null, waits: [], background_helpers: [], goal: null,
      active_assistant_message_id: state.steerRequested ? 'steer-repro-a2' : before.id,
    })
    const detail = () => ({
      ...chat, ...runtime(), messages: state.messages, total: state.messages.length,
      offset: 0, active_goal_objective: null, active_goal: null, goal_id: null,
    })
    const json = value => new Response(JSON.stringify(value), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })
    const emit = event => state.controller.enqueue(new TextEncoder().encode(
      `data: ${JSON.stringify(event)}\n\n`,
    ))
    window.fetch = async (input, init = {}) => {
      const url = new URL(String(input?.url || input), location.href)
      const method = init.method || input?.method || 'GET'
      if (url.pathname === `${target}/stream`) {
        // Keep the live body open; route.fulfill would finish the turn.
        return new Response(new ReadableStream({ start(controller) {
          state.controller = controller
          emit({ type: 'catch_up_done' })
        } }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      if (url.pathname === `${target}/runtime`) return json(runtime())
      if (url.pathname === target && method === 'GET') return json(detail())
      if (url.pathname === `${target}/pending/${sibling.cid}` && method === 'PATCH') {
        const edited = { ...sibling, content: JSON.parse(init.body).content }
        state.patchRequested = true
        state.pending = [steered, edited]
        // The server captured this list before the steer; delay only its delivery.
        return new Promise(resolve => {
          state.releasePatch = () => resolve(json({
            updated: true, pending_messages: [steered, edited],
          }))
        })
      }
      if (url.pathname === `${target}/messages` && method === 'POST') {
        const body = JSON.parse(init.body)
        if (!body.force_steer || body.consume_pending_cids?.[0] !== steered.cid) {
          throw new Error('Fixture blocks unexpected sends')
        }
        state.steerRequested = true
        state.pending = state.pending.filter(row => row.cid !== steered.cid)
        state.messages = [initial, before, { ...steered, steered: true }]
        emit({ type: 'steered_into_turn', messages: [steered],
          assistant_message_id: before.id, sealed_items: before.blocks,
          next_assistant_message_id: 'steer-repro-a2', items: [] })
        emit({ type: 'text', content: 'I received your steer and will check the earlier answers.' })
        return json({ status: 'steered', cut_deferred: true,
          pending_messages: [steered, sibling] })
      }
      // This fixture must never invoke a real provider or change owner data.
      if (url.pathname.startsWith('/api/') && !['GET', 'HEAD'].includes(method)) {
        throw new Error(`Read-only fixture blocked ${method} ${url.pathname}`)
      }
      return nativeFetch(input, init)
    }
  }, { chat })
}
