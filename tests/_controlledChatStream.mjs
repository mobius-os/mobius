// route.fulfill() ends its response even without `done`. Use a real open body
// for scenarios whose assertions must run before either SSE termination or EOF.
export async function installControlledChatStream(page, text) {
  await page.addInitScript(initialText => {
    const nativeFetch = window.fetch.bind(window)
    const fixture = window.__controlledChatStream = {
      armed: false, requests: 0, open: false, terminals: 0, aborts: 0,
    }
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input?.url
      if (!fixture.armed || !/\/api\/chats\/[0-9a-f-]+\/stream$/.test(String(url))) {
        return nativeFetch(input, init)
      }
      fixture.requests++
      const signal = init?.signal || input?.signal
      const encoder = new TextEncoder()
      let abort
      const body = new ReadableStream({
        start(controller) {
          fixture.open = true
          fixture.emit = event => {
            if (!fixture.open) throw new Error('Controlled stream is not open')
            controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
          }
          fixture.finish = () => {
            fixture.emit({ type: 'done' })
            fixture.terminals++
            fixture.open = false
            signal?.removeEventListener('abort', abort)
            controller.close()
          }
          abort = () => {
            fixture.aborts++
            fixture.open = false
            controller.error(new DOMException('Aborted', 'AbortError'))
          }
          if (signal?.aborted) return abort()
          signal?.addEventListener('abort', abort, { once: true })
          fixture.emit({ type: 'catch_up_done' })
          fixture.emit({ type: 'text', content: initialText })
        },
        cancel() {
          fixture.open = false
          signal?.removeEventListener('abort', abort)
        },
      })
      return Promise.resolve(new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      }))
    }
  }, text)
  return {
    arm: () => page.evaluate(() => { window.__controlledChatStream.armed = true }),
    emit: event => page.evaluate(value => window.__controlledChatStream.emit(value), event),
    finish: () => page.evaluate(() => window.__controlledChatStream.finish()),
    state: () => page.evaluate(() => {
      const { requests, open, terminals, aborts } = window.__controlledChatStream
      return { requests, open, terminals, aborts }
    }),
  }
}
