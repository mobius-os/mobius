/* App block parts. A static block is a conversation-authored snapshot with
   links into the app; nothing app-owned runs until the reader explicitly
   opens the embedded view. */
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowDown, ArrowUpRight, Check, ChevronDown, ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { appFrameVersion } from '../../../lib/appVersion.js'
import { sharedBrowserShellHref } from '../../../lib/sharedBrowserWorkspace.js'
import { openInternalLink } from '../internalLink.js'
import { openExistingChat } from '../../../lib/appChatNavigation.js'
import { appBlockHref, transcriptHostRequest } from './appBlock.js'
import './AppBlock.css'

export const AppCanvas = lazy(() => import('../../AppCanvas/AppCanvas.jsx'))

export function installedApp(apps, slug) {
  return (apps || []).find(app => app.slug === slug && !app.deleted_at) || null
}

/** Navigation the embedded view may request, through the shell's navigator.
 *  `composer` carries an unsent draft already staged for the opened chat. */
export function useTranscriptNavigation(onInternalNav) {
  return useCallback((target, composer) => {
    const url = new URL(sharedBrowserShellHref(target), window.location.href)
    if (onInternalNav) onInternalNav(url, composer)
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

/** App-reported links open outside Möbius, marked by an arrow. */
export function ExternalLinks({ links }) {
  if (!links.length) return null
  return <span className="md-app-block__links">{links.map(link =>
    <a key={link.url} className="md-app-block__link" href={link.url} target="_blank" rel="noopener noreferrer">
      {link.label}<ArrowUpRight width={14} height={14} aria-hidden="true" /></a>)}</span>
}

/** A short app-reported status as a pill; `busy` adds a small spinner. */
export function StatusPill({ status, tone = 'neutral', busy = false }) {
  if (!status && !busy) return null
  return <span className={`md-app-block__pill is-${tone}`}>
    {busy ? <span className="md-app-block__spinner" aria-hidden="true" /> : null}
    {status || <span className="md-app-block__sr-only">Working</span>}</span>
}

/** A row's live state from a passive session (appBlockSession.js sessionItems). */
function ItemState({ live }) {
  if (!live) return null
  return <span className="md-app-block__item-state">
    <ExternalLinks links={live.links} />
    <StatusPill status={live.status} tone={live.statusTone} busy={live.busy} />
  </span>
}

/** A live title's state mark (`titleTone`, `titleBusy`): a spinner, a check
 *  for success or accent, an exclamation mark for attention or danger, and
 *  nothing for neutral. The title's words carry the meaning; this only marks
 *  it. */
export function TitleMark({ tone = 'neutral', busy = false }) {
  if (!busy && !['success', 'accent', 'attention', 'danger'].includes(tone)) return null
  return <span className={`md-app-block__title-mark is-${busy ? 'neutral' : tone}`} aria-hidden="true">
    {busy ? <span className="md-app-block__spinner" />
      : tone === 'success' || tone === 'accent' ? <Check width={14} height={14} />
        : <span className="md-app-block__title-mark-alert">!</span>}</span>
}

/** Every shape says why it offers no app view when its app is missing. */
export function UnavailableNotice({ slug, loading }) {
  return <p className="md-app-block__notice" role="status">
    {loading ? 'Checking installed apps…' : `${slug} is not installed. The saved snapshot remains here.`}</p>
}

/** The block's rows. A passive session's confirmation rows (sessionSnapshot)
 *  come from the app and link nowhere; `listEdgeRefs` marks their first and
 *  last row so Confirm can require both ends to have been on screen. */
export function BlockItems({ items, onInternalNav, actionFor, listEdgeRefs = null }) {
  if (!items.length) return null
  const first = items.findIndex(item => item.confirmation)
  const last = items.findLastIndex(item => item.confirmation)
  return <ul className="md-app-block__items">{items.map((item, i) => <li key={i} className={item.confirmation ? 'is-confirmation' : undefined}>
    {i === first ? <span ref={listEdgeRefs?.start} className="md-app-block__list-edge is-start" aria-hidden="true" /> : null}
    <div className="md-app-block__item-main">
      {item.confirmation || (item.appRow && !item.href)
        ? <span className="md-app-block__item-title">{item.title}</span>
        : <AppLink className="md-app-block__item-title" href={item.href} onInternalNav={onInternalNav}>{item.title}</AppLink>}
      <Badges badges={item.badges} />
      <Facts facts={item.facts} />
      {item.appRow && item.meta ? <p className="md-app-block__item-meta">{item.meta}</p> : null}
      {item.live?.note ? <p className="md-app-block__item-note">{item.live.note}</p> : null}
    </div>
    <ItemState live={item.live} />
    {actionFor(item.action)}
    {i === last ? <span ref={listEdgeRefs?.end} className="md-app-block__list-edge is-end" aria-hidden="true" /> : null}
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
  summary = '', sectionRef = null, listEdgeRefs = null, onJump = null, ask = null, children = null }) {
  // A host-drawn ask (PassiveAppBlock.jsx) takes the place of the block's own
  // action, so the block still offers one action.
  const action = ask || actionFor(block.action)
  const valueFacts = block.itemFacts === 'values' ? ' md-app-block--value-facts' : ''
  // An app that reports no rows and shows no control folds its block to one
  // quiet line (for example "Continued below" on an earlier card).
  const folded = block.appRows && !block.items.length && !action && !toggle && !summary
  return <section ref={sectionRef} className={`md-app-block${valueFacts}${folded ? ' md-app-block--folded' : ''}`} aria-label={block.title}>
    <header>
      <TitleMark tone={block.titleTone} busy={block.titleBusy} />
      <strong>{block.title}</strong>
      <Badges badges={block.badges} />
      {folded && onJump ? <button type="button" className="md-app-block__jump" onClick={onJump}>
        Jump to latest<ArrowDown width={14} height={14} aria-hidden="true" /></button> : null}
      {/* A folded line that jumps to its newest sibling needs no second link. */}
      {folded && onJump ? null : <AppLink className="md-app-block__open" href={block.href} onInternalNav={onInternalNav}>
        Open{app ? ` in ${app.name}` : ''}<ChevronRight width={16} height={16} aria-hidden="true" /></AppLink>}
    </header>
    {/* While the rows are the app's confirmation or its own rows, nothing is
        added around them. A live title is the app's; saved rows are still the
        conversation's. */}
    {block.confirmation || block.appRows ? null
      : <p className="md-app-block__source">{block.liveTitle ? 'Rows from this conversation' : 'From this conversation'}</p>}
    {summary ? <p className="md-app-block__summary" role="status">{summary}</p> : null}
    <Facts facts={block.facts} />
    <BlockItems items={block.items} onInternalNav={onInternalNav} actionFor={actionFor} listEdgeRefs={listEdgeRefs} />
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
  const onHostRequest = useCallback((_, request) => {
    // Only the strict request may leave a draft from a transcript; it reports
    // its outcome to the app instead of falling back.
    if (request?.type === 'moebius:open-chat' && request.requireExisting) {
      const target = `/shell/?${new URLSearchParams({ chat: request.chatId })}`
      return openExistingChat(request, draft => navigate(target, draft === undefined ? undefined : { draft }))
    }
    return transcriptHostRequest(request, { navigate })
  }, [navigate])
  const actionable = block.inline && app
  // `expand: false`: no inline view at all; an action is a link into the app.
  const openable = actionable && block.expand
  const toggleView = intent => setViewIntent(current => current === intent ? null : intent)
  const actionFor = target => {
    if (!actionable || !target) return null
    if (!openable) {
      return <AppLink className="md-app-block__action" href={appBlockHref(block.app, target.intent)}
        onInternalNav={onInternalNav}>{target.label}</AppLink>
    }
    return <button type="button" className="md-app-block__action" aria-pressed={viewIntent === target.intent}
      onClick={() => toggleView(target.intent)}>{target.label}</button>
  }
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
