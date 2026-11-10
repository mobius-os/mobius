/* MessageBranchButton — "Branch in new chat" beside a settled reply's Copy.
   `branch()` resolves true once the new chat is open; the Shell reports a
   failure itself, so this only shows that work is under way and refuses a
   second tap meanwhile. */
import { useState } from 'react'
import { Branch } from '@openai/apps-sdk-ui/components/Icon'

export default function MessageBranchButton({ branch }) {
  const [branching, setBranching] = useState(false)

  async function handleBranch(e) {
    // The containing message row owns reveal timing (see MessageCopyButton).
    e.stopPropagation()
    if (branching) return
    setBranching(true)
    try {
      await branch()
    } finally {
      setBranching(false)
    }
  }

  return (
    <button
      type="button"
      className={`chat__msg-branch${branching ? ' chat__msg-branch--busy' : ''}`}
      onClick={handleBranch}
      aria-label="Branch in new chat"
      aria-busy={branching || undefined}
      title="Branch in new chat"
    >
      <Branch width={14} height={14} aria-hidden="true" />
    </button>
  )
}
