/* First-run agent setup: the same provider sign-in flows as Settings. Both providers stay as tabs,
   each with its own state, so connecting one shows its success on that tab only while the other
   can still be connected. */
import { useState } from 'react'
import { authQueries } from '../../hooks/queries.js'
import { configuredProviderSet } from '../../lib/providerAvailability.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import { CheckIcon } from './WalkthroughIcons.jsx'

const PROVIDERS = [['codex', 'OpenAI Codex'], ['claude', 'Claude Code']]

export default function WalkthroughSetup() {
  const statusQuery = authQueries.provider.statuses.useQuery()
  const configured = configuredProviderSet(statusQuery.data)
  const [choice, setChoice] = useState('codex')
  const name = PROVIDERS.find(([id]) => id === choice)[1]

  return <div className="wt-setup">
    {statusQuery.isError ? <div className="wt-note" role="alert">Connection status is unavailable right now. You can try again in Settings.</div>
      : statusQuery.isPending ? <div className="wt-note" role="status">Checking your connections…</div>
        : <>
          <div className="wt-setup__choices" role="group" aria-label="Choose an AI provider">
            {PROVIDERS.map(([id, label]) => <button key={id} type="button" aria-pressed={choice === id} className={choice === id ? 'is-selected' : ''} onClick={() => setChoice(id)}>
              {label}{configured.has(id) && <span className="wt-setup__tick" role="img" aria-label="connected"><CheckIcon size={11} /></span>}
            </button>)}
          </div>
          <div className="wt-setup__form">
            {configured.has(choice)
              ? <div className="wt-success" role="status"><CheckIcon size={14} /> {name} is connected and ready for Chat.</div>
              : choice === 'codex' ? <CodexAuth /> : <ProviderAuth authenticated={false} compact />}
          </div>
        </>}
  </div>
}
