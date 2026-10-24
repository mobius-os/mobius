/* A `mobius-app` block in an assistant reply. See building-apps.md. */
import { useState } from 'react'
import { appQueries } from '../../../hooks/queries.js'
import { StaticAppBlock, installedApp } from './AppBlockParts.jsx'
import { passiveAppBlockAllowed } from './appBlockSession.js'
import PassiveAppBlock from './PassiveAppBlock.jsx'
import { appBlockRoute } from './blockOwnership.js'

export { appBlockRoute }


export default function AppBlock({ block, onInternalNav }) {
  const apps = appQueries.list.useQuery()
  const app = installedApp(apps.data, block.app)
  const admitted = passiveAppBlockAllowed(app)
  const [started, setStarted] = useState(false)
  // Derived during render, so the very render that withdraws admission still
  // routes to the session that already owns the block.
  if (block.session && admitted && !started) setStarted(true)
  if (appBlockRoute({ requested: block.session, installed: Boolean(app), admitted, started }) === 'passive') {
    return <PassiveAppBlock block={block} app={app} onInternalNav={onInternalNav} admitted={admitted} />
  }
  return <StaticAppBlock block={block} app={app} appsLoading={apps.isLoading} onInternalNav={onInternalNav} />
}
