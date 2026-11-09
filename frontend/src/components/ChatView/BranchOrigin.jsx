/* BranchOrigin — the "Branched from <chat>" divider a branch shows where its
   copied history ends (Chat.branch_origin.through_count). The source title
   opens that chat while it still exists. */
import { Branch } from '@openai/apps-sdk-ui/components/Icon'

export default function BranchOrigin({ origin, onInternalNav }) {
  const title = origin.title || 'another chat'
  const open = origin.available && onInternalNav
    ? () => onInternalNav(new URL(
      `/shell/?chat=${encodeURIComponent(origin.chat_id)}`, window.location.origin,
    ))
    : null
  return (
    <li className="chat__branch-origin" data-key="branch-origin">
      <span className="chat__branch-origin-label">
        <Branch width={13} height={13} aria-hidden="true" />
        <span>Branched from </span>
        {open
          ? <button type="button" className="chat__branch-origin-link" onClick={open}>{title}</button>
          : <span className="chat__branch-origin-title">{title}</span>}
      </span>
    </li>
  )
}
