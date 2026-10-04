/* One themed group of apps, drawn as App Store listings. */
import { StoreAppCard } from './WalkthroughStore.jsx'

export default function WalkthroughAppGroup({ group, store, statusOf, onInstall }) {
  const failed = group.apps.map(app => statusOf(app.id)).find(status => status?.state === 'error')
  return <div className="wt-group">
    {store.catalogError && <p className="wt-note" role="status">{store.catalogError}</p>}
    <div className="wt-group__grid" style={{ '--wt-cols': group.apps.length === 4 ? 2 : group.apps.length }}>
      {group.apps.map(app => <StoreAppCard key={app.id} app={app} store={store} status={statusOf(app.id)} onInstall={onInstall} />)}
    </div>
    {failed && <p className="wt-note" role="alert">{failed.error}</p>}
  </div>
}
