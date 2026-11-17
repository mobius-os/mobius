/** Browser fixture for the real composer without an account, network, or agent execution. */
import React, { useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatInputBar from '../ChatInputBar.jsx'
import BrainUsageIcon from '../BrainUsageIcon.jsx'
import { editorText, editorSelection, selectEditorRange } from '../textEditor.js'
import { reconcileComposerTextarea } from '../composerTextareaSizing.js'
import '../ChatView.css'
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
function Fixture() {
  const [input, setInput] = useState('')
  const [files, setFiles] = useState([])
  const inputRef = useRef(null)
  const submits = useRef([])
  const intents = useRef([])
  const send = event => { event?.preventDefault?.(); submits.current.push(input); setInput('') }
  window.composerFixture = {
    text: () => editorText(inputRef.current),
    selection: () => editorSelection(inputRef.current),
    select: (from, to) => selectEditorRange(inputRef.current, from, to),
    replace: setInput,
    attachments: setFiles,
    reconcile: () => reconcileComposerTextarea(inputRef.current),
    submissions: submits.current,
    intents: intents.current,
  }
  return <div className="chat" style={{ '--composer-room': '600px', width: 'min(600px, 100%)' }}>
    <ChatInputBar chatId="fixture" input={input} inputRef={inputRef} onInputChange={setInput}
      onInputIntent={event => intents.current.push(event.type)}
      onSubmit={send} onSubmitSteer={send} onToggleVoice={() => {}}
      messageHistory={[{ key: 'old', kind: 'sent', text: 'older **message**' }]}
      leftButtons={<div className="composer-plus"><button type="button" className="chat__plus chat__brain-usage" aria-label="Composer settings"><BrainUsageIcon leftPercent={50} rightPercent={25} /></button></div>}
      pendingFiles={files} onRemoveFile={id => setFiles(current => current.filter(file => file.id !== id))} />
  </div>
}
createRoot(document.getElementById('root')).render(
  <QueryClientProvider client={client}><Fixture /></QueryClientProvider>,
)
