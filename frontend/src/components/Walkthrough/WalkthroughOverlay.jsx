/* First-run guide: a dismissible modal dialog over the shell that introduces Möbius, sets up the
   owner's profile and agent, and tours the apps. It borrows mobius.you's look: flat dark surfaces,
   big tight headlines with a lavender second phrase, and quiet bordered cards. */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { ownerQueries } from '../../hooks/queries.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { escapeShouldDismissGuide } from './guideEscape.js'
import WalkthroughSetup from './WalkthroughSetup.jsx'
import { AgentBrainFlow, AgentChatDemo } from './WalkthroughAgent.jsx'
import { ThemeChangeDemo } from './WalkthroughTheme.jsx'
import WalkthroughAppGroup from './WalkthroughAppGroup.jsx'
import WalkthroughInstall from './WalkthroughInstall.jsx'
import { Reveal, Typewriter, WordReveal } from './WalkthroughMotion.jsx'
import WalkthroughProfile, { useAccountProfile } from './WalkthroughProfile.jsx'
import WalkthroughAccessConfirm from './WalkthroughAccessConfirm.jsx'
import WalkthroughStore, { STORE_WINDOW_APPS, useStoreCatalog } from './WalkthroughStore.jsx'
import { useAppInstall } from './useAppInstall.js'
import { APP_GROUPS } from './walkthroughGroups.js'
import './WalkthroughOverlay.css'
import './WalkthroughScreens.css'

// `title` is [plain, accent]: the second phrase is set in the accent color.
const SCREENS = [
  { id: 'welcome', eyebrow: 'Welcome', title: ['Welcome to Möbius.', 'Your personal agent.'], lead: 'Möbius is your personal agent. It is the interface for the AI agents you choose, with chat, memory, and the apps they build all in one workspace.', typedLead: true, content: 'profile' },
  { id: 'agent', eyebrow: 'Meet your agent', title: ['Say what you need.', 'Get a working app.'], lead: 'Describe it like you would to a friend. Attach images, PDFs, or code if it helps. Your agent builds it and shows you the result.', content: 'chat' },
  { id: 'shape', eyebrow: 'Shape Möbius', title: ['Your workspace.', 'Your rules.'], lead: 'Möbius is built to be reshaped. Ask your agent for a new theme, a different layout, or a feature you wish it had, and it changes the app you are using, right away.', content: 'theme' },
  { id: 'brain', eyebrow: 'How it thinks', title: ['One agent.', 'Endless errands.'], lead: 'Behind every chat sits your agent. It does the work, then hands you something real.', content: 'brain' },
  { id: 'store', eyebrow: 'App Store', title: ['Grab an app.', 'Make it yours.'], lead: 'Install what you need, publish what you build, and ask your agent to change anything.', content: 'store' },
  ...APP_GROUPS.map(group => ({ id: group.id, eyebrow: group.eyebrow, title: group.title, lead: group.lead, content: 'group', group })),
  { id: 'connect', eyebrow: 'Your agent', title: ['Bring your', 'own agent.'], lead: 'An agent powers everything you just saw. Pick a provider and sign in. You can add more agents, switch models, or disconnect anytime in Settings under AI providers.', content: 'connect' },
  { id: 'finish', eyebrow: 'All set', title: ['Good luck.', 'Enjoy Möbius.'], lead: 'Your guide is done. Start in Chat and ask for anything.', content: 'finish' },
]
const LAST = SCREENS.length - 1
const CONNECT_INDEX = SCREENS.findIndex(item => item.content === 'connect')

// Bars that change on a jump do so in order, starting next to where the guide was: forward fills them
// left to right, backward empties them right to left.
function barRippleDelay(from, to, index) {
  if (to > from && index > from && index <= to) return (index - from - 1) * 70
  if (to < from && index > to && index <= from) return (from - index) * 70
  return 0
}

// How each screen's title and intro arrive. Neighbouring screens never repeat, so the guide keeps
// feeling fresh, but every effect is short and quiet (see Reveal in WalkthroughMotion).
const MOTION = {
  welcome: { title: 'mask', lead: 'typed' },
  agent: { title: 'rise', lead: 'rise' },
  shape: { title: 'mask', lead: 'blur' },
  brain: { title: 'blur', lead: 'words' },
  store: { title: 'drift', lead: 'blur' },
  system: { title: 'mask', lead: 'rise' },
  personalize: { title: 'rise', lead: 'words' },
  artifacts: { title: 'blur', lead: 'rise' },
  explore: { title: 'rise', lead: 'words' },
  insight: { title: 'drift', lead: 'blur' },
  connect: { title: 'mask', lead: 'rise' },
  finish: { title: 'rise', lead: 'words' },
}

function LeadText({ kind, text }) {
  if (kind === 'typed') return <Typewriter text={text} speed={11} startDelay={200} />
  if (kind === 'words') return <WordReveal text={text} delay={150} stagger={30} />
  return <Reveal kind={kind} delay={220}>{text}</Reveal>
}

// Two block lines, plain then accent, so a phrase never breaks across them.
function Title({ kind, plain, accent }) {
  return <>
    <span className="wt__title-line"><Reveal kind={kind} delay={60}>{plain}</Reveal></span>
    <span className="wt__title-line is-accent"><Reveal kind={kind} delay={190}>{accent}</Reveal></span>
  </>
}

export default function WalkthroughOverlay({ apps, activeAppId = null, onOpenApp, onHandoffChange }) {
  const queryClient = useQueryClient()
  const cardRef = useRef(null)
  const titleRef = useRef(null)
  const [stepIndex, setStepIndex] = useState(0)
  // Where the guide was, so a jump across several screens ripples through the bars one by one.
  const previousStepRef = useRef(0)
  const barFrom = previousStepRef.current
  useEffect(() => { previousStepRef.current = stepIndex }, [stepIndex])
  const [handoffAppId, setHandoffAppId] = useState(null)
  const [direction, setDirection] = useState(1)
  const screen = SCREENS[stepIndex]
  const [plainTitle, accentTitle] = screen.title
  // The guide steps aside while the owner works in the app it handed them to.
  const suspended = handoffAppId != null && activeAppId != null && String(activeAppId) === String(handoffAppId)
  const wantedIconIds = screen.group ? screen.group.apps.map(app => app.id) : screen.content === 'store' ? STORE_WINDOW_APPS.map(app => app.id) : []
  const store = useStoreCatalog(apps, wantedIconIds)
  const installer = useAppInstall(store.catalog)
  const confirming = installer.confirmation
  const confirmingApp = confirming ? APP_GROUPS.flatMap(group => group.apps).find(app => app.id === confirming.id) : null
  const identityApp = apps.find(app => app.slug === 'identity') || null
  // One profile for the whole guide, so Back to the welcome screen never refetches it or loses who claimed what.
  const account = useAccountProfile(() => { if (identityApp) openApp(identityApp.id) })
  const reloadAccount = account.reload

  // The shell's history restore must not queue chat-composer focus while this
  // guide is handing back from another app. Publish the lease at the same commit
  // that hides the guide, and release it on return or unmount.
  useLayoutEffect(() => {
    onHandoffChange?.(suspended)
    return () => { if (suspended) onHandoffChange?.(false) }
  }, [onHandoffChange, suspended])

  // Back from the hand-off, the guide opens again where the owner left it.
  const wasSuspendedRef = useRef(false)
  useEffect(() => {
    if (wasSuspendedRef.current && !suspended) {
      setHandoffAppId(null)
      reloadAccount()
    }
    wasSuspendedRef.current = suspended
  }, [suspended, reloadAccount])

  // A modal dialog: focus starts on the title, Tab stays inside the card, the page behind is inert,
  // and focus returns to where it was when the guide goes away. Escape dismisses it exactly like the
  // close button, except while typing in a field. (While an access confirmation is open, Escape belongs to that popup and only cancels it.)
  useDialogFocus({ open: !suspended, containerRef: cardRef, initialFocusRef: titleRef, onClose: finish, shouldCloseOnEscape: escapeShouldDismissGuide })
  // Each screen announces itself by moving focus to its title.
  useEffect(() => { titleRef.current?.focus({ preventScroll: true }) }, [stepIndex])

  function finish() {
    queryClient.setQueryData(ownerQueries.walkthrough.key, previous => ({
      ...(previous || { completed_at: null }), completed: true,
    }))
    try { localStorage.setItem('mobius:walkthrough-completed', '1') } catch (_) {}
    api.owner.walkthrough.complete().catch(() => {})
  }

  function goTo(index) {
    const next = Math.max(0, Math.min(LAST, index))
    setDirection(next >= stepIndex ? 1 : -1)
    installer.dismiss()
    setStepIndex(next)
  }

  function openApp(appId, intent) {
    setHandoffAppId(appId)
    void onOpenApp(appId, intent)
  }

  if (suspended) return null

  return <>
    <div className="wt__backdrop" aria-hidden="true" />
    <div className="wt__card" ref={cardRef} data-screen={screen.id} role="dialog" aria-modal="true" aria-labelledby="wt-title">
      <div className="wt__topline">
        <div className="wt__brand"><img src="/moebius.png" alt="" width="28" height="28" /><strong>Möbius</strong><span>Getting started</span></div>
        <span className="wt__count"><span aria-hidden="true">{stepIndex + 1} of {SCREENS.length}</span><span className="sr-only">Step {stepIndex + 1} of {SCREENS.length}</span></span>
        <button type="button" className="wt__close" onClick={finish} aria-label="Dismiss welcome" title="Dismiss guide">×</button>
      </div>
      <div className="wt__bars" role="group" aria-label="Guide progress">
        {SCREENS.map((item, index) => <button key={item.id} type="button" style={{ '--wt-bar-delay': `${barRippleDelay(barFrom, stepIndex, index)}ms` }} className={index === stepIndex ? 'is-current' : index < stepIndex ? 'is-seen' : ''} aria-label={`Go to ${item.eyebrow}`} aria-current={index === stepIndex ? 'step' : undefined} onClick={() => goTo(index)} />)}
      </div>
      <div className={`wt__slide ${direction < 0 ? 'is-back' : 'is-forward'}`} role="region" aria-labelledby="wt-title" tabIndex={0} key={screen.id}>
        <h2 id="wt-title" ref={titleRef} tabIndex={-1}><Title kind={(MOTION[screen.id] || MOTION.agent).title} plain={plainTitle} accent={accentTitle} /></h2>
        <p className="wt__lead"><LeadText kind={(MOTION[screen.id] || MOTION.agent).lead} text={screen.lead} /></p>
        {screen.content === 'profile' && <WalkthroughProfile identityApp={identityApp} controller={account} />}
        {screen.content === 'chat' && <AgentChatDemo />}
        {screen.content === 'theme' && <ThemeChangeDemo />}
        {screen.content === 'brain' && <AgentBrainFlow />}
        {screen.content === 'store' && <WalkthroughStore store={store} />}
        {screen.content === 'group' && <WalkthroughAppGroup group={screen.group} store={store} statusOf={installer.statusOf} locked={installer.busy} onInstall={installer.begin} />}
        {screen.content === 'connect' && <WalkthroughSetup />}
        {screen.content === 'finish' && <>
          <WalkthroughInstall />
          <ul className="wt-next">
            <li><strong>Chat</strong><span>Ask for anything. An app, an answer, or a plan.</span></li>
            <li><strong>App Store</strong><span>Add apps whenever you feel curious.</span></li>
            <li><strong>Settings</strong><span>Switch agents or models and change the look.</span></li>
          </ul>
        </>}
      </div>
      {confirming && confirmingApp && <WalkthroughAccessConfirm confirmation={confirming} app={confirmingApp} icon={store.icons[confirmingApp.id]} onApprove={installer.approve} onCancel={installer.dismiss} />}
      <div className="wt__footer">
        {stepIndex > 0 && <button type="button" className="wt__back" onClick={() => goTo(stepIndex - 1)}>Back</button>}
        {screen.content === 'profile' && account.returning && <button type="button" className="wt__back" onClick={() => goTo(CONNECT_INDEX)}>Skip to agent setup</button>}
        <button type="button" className="wt__next" onClick={() => stepIndex === LAST ? finish() : goTo(stepIndex + 1)}>
          {stepIndex === LAST ? 'Finish guide' : 'Continue'}<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 8h10M9 4l4 4-4 4" /></svg>
        </button>
      </div>
    </div>
  </>
}
