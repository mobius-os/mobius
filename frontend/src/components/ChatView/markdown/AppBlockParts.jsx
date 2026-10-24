/* App block parts. A static block is a conversation-authored snapshot with
   links into the app; nothing app-owned runs until the reader explicitly
   opens the embedded view. */
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { appFrameVersion } from '../../../lib/appVersion.js'
import { sharedBrowserShellHref } from '../../../lib/sharedBrowserWorkspace.js'
import { openInternalLink } from '../internalLink.js'
import { transcriptHostRequest } from './appBlock.js'
import './AppBlock.css'

export const AppCanvas = lazy(() => import('../../AppCanvas/AppCanvas.jsx'))

export function installedApp(apps, slug) {
  return (apps || []).find(app => app.slug === slug && !app.deleted_at) || null
}

/** Navigation the embedded view may request, through the shell's navigator. */
export function useTranscriptNavigation(onInternalNav) {
  return useCallback(target => {
    const url = new URL(sharedBrowserShellHref(target), window.location.href)
    if (onInternalNav) onInternalNav(url)
    else window.location.assign(url.href)
  }, [onInternalNav])
}

export function AppLink({ href, onInternalNav, className, children }) {
  const target = sharedBrowserShellHref(href)
  return <a className={className} href={target}
    onClick={event => openInternalLink(event, target, onInternalNav)}>{children}</a>
}

export function Badges({ badges }) {
  if (!badges.length) return null
  return <span className="md-app-block__badges">{badges.map((badge, i) =>
    <span key={i} className={`md-app-block__badge is-${badge.tone}`}>{badge.label}</span>)}</span>
}

export function Facts({ facts }) {
  if (!facts.length) return null
  return <dl className="md-app-block__facts">{facts.map((fact, i) => <div key={i}>
    <dt>{fact.label}</dt>
    <dd>{fact.href ? <a href={fact.href} target="_blank" rel="noopener noreferrer">{fact.value}</a> : fact.value}</dd>
  </div>)}</dl>
}

/** Every shape says why it offers no app view when its app is missing. */
export function UnavailableNotice({ slug, loading }) {
  return <p className="md-app-block__notice" role="status">
    {loading ? 'Checking installed apps…' : `${slug} is not installed. The saved snapshot remains here.`}</p>
}

export function BlockItems({ items, onInternalNav, actionFor }) {
  if (!items.length) return null
  return <ul className="md-app-block__items">{items.map((item, i) => <li key={i}>
    <div className="md-app-block__item-main">
      <AppLink className="md-app-block__item-title" href={item.href} onInternalNav={onInternalNav}>{item.title}</AppLink>
      <Badges badges={item.badges} />
      <Facts facts={item.facts} />
    </div>
    {actionFor(item.action)}
  </li>)}</ul>
}

/** The app's ordinary visible view, mounted only after the reader asks, with
 *  one pending intent delivered exactly once. */
export function AppBlockView({ app, intent, height, onHostRequest }) {
  const pending = useMemo(() => ({ intent, nonce: crypto.randomUUID() }), [intent])
  const [delivered, setDelivered] = useState(false)
  return <div className="md-app-block__view" style={{ height }}>
    <Suspense fallback={<p role="status">Opening {app.name}…</p>}>
      <AppCanvas appId={app.id} appName={app.name} appSlug={app.slug} version={appFrameVersion(app)}
        storageGeneration={app.storage_generation} offlineCapable={app.offline_capable === true}
        capabilityContract={app.capability_contract || null} active={false} visible interactive
        navigationTarget={false} pendingIntent={delivered ? null : pending} onIntentDelivered={() => setDelivered(true)}
        onHostRequest={onHostRequest} />
    </Suspense>
  </div>
}

/** Shared block chrome. `actionFor` renders the control for one app action;
 *  `summary` is optional app-reported text shown under the saved title. */
export function AppBlockLayout({ block, app, appsLoading, onInternalNav, actionFor, toggle = null,
  summary = '', sectionRef = null, children = null }) {
  const action = actionFor(block.action)
  return <section ref={sectionRef} className="md-app-block" aria-label={block.title}>
    <header>
      <strong>{block.title}</strong>
      <Badges badges={block.badges} />
      <AppLink className="md-app-block__open" href={block.href} onInternalNav={onInternalNav}>
        Open{app ? ` in ${app.name}` : ''}<ChevronRight width={16} height={16} aria-hidden="true" /></AppLink>
    </header>
    <p className="md-app-block__source">From this conversation</p>
    {summary ? <p className="md-app-block__summary" role="status">{summary}</p> : null}
    <Facts facts={block.facts} />
    <BlockItems items={block.items} onInternalNav={onInternalNav} actionFor={actionFor} />
    {action || toggle ? <footer>{toggle}{action}</footer> : null}
    {app ? null : <UnavailableNotice slug={block.app} loading={appsLoading} />}
    {children}
  </section>
}

export function StaticAppBlock({ block, app, appsLoading, onInternalNav, sectionRef = null, onOpenChange = null }) {
  const [viewIntent, setViewIntent] = useState(null)
  // A passive host keeps this block static while the reader has a view open.
  const open = viewIntent !== null
  useEffect(() => { onOpenChange?.(open) }, [onOpenChange, open])
  const navigate = useTranscriptNavigation(onInternalNav)
  const onHostRequest = useCallback((_, request) => transcriptHostRequest(request, { navigate }), [navigate])
  const openable = block.inline && app
  const toggleView = intent => setViewIntent(current => current === intent ? null : intent)
  const actionFor = target => openable && target
    ? <button type="button" className="md-app-block__action" aria-pressed={viewIntent === target.intent}
      onClick={() => toggleView(target.intent)}>{target.label}</button>
    : null
  const toggle = openable
    ? <button type="button" className="md-app-block__toggle" aria-expanded={viewIntent !== null}
      onClick={() => setViewIntent(current => current === null ? block.intent : null)}>
      <ChevronDown width={16} height={16} aria-hidden="true" />
      {viewIntent !== null ? 'Hide details' : block.expandLabel || 'Show details here'}</button>
    : null
  return <AppBlockLayout block={block} app={app} appsLoading={appsLoading} onInternalNav={onInternalNav}
    actionFor={actionFor} toggle={toggle} sectionRef={sectionRef}>
    {openable && viewIntent !== null
      ? <AppBlockView key={viewIntent} app={app} intent={viewIntent} height={block.height} onHostRequest={onHostRequest} />
      : null}
  </AppBlockLayout>
}
