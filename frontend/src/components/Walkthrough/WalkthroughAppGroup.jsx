/* One themed group of apps, drawn as App Store listings. */
import { StoreAppCard } from './WalkthroughStore.jsx'

// One row for up to three apps, two columns for four, three for more (so five wrap 3 + 2).
const columnsFor = count => (count <= 3 ? count : count === 4 ? 2 : 3)

export default function WalkthroughAppGroup({ group, store, statusOf, locked, onInstall }) {
  const failed = group.apps.map(app => statusOf(app.id)).find(status => status?.state === 'error')
  const warned = group.apps.map(app => statusOf(app.id)).find(status => status?.state === 'installed' && status.warnings?.length)
  return <div className="wt-group">
    {store.catalogError && <p className="wt-note" role="status">{store.catalogError}</p>}
    <div className="wt-group__grid" style={{ '--wt-cols': columnsFor(group.apps.length) }}>
      {group.apps.map(app => <StoreAppCard key={app.id} app={app} store={store} status={statusOf(app.id)} locked={locked} onInstall={onInstall} />)}
    </div>
    {warned && <p className="wt-note" role="status">{warned.warnings.join(' ')}</p>}
    {failed && <p className="wt-note" role="alert">{failed.error}</p>}
  </div>
}
