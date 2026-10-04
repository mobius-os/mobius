/* Two looping explainers for the agent screens, built from real React
   components rather than recordings. The chat is a close copy of the real
   Möbius chat with an app opening beside it, and the "brain" flow shows how
   one agent handles different requests. */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import BrainUsageIcon from '../ChatView/BrainUsageIcon.jsx'
import { ChevronDown, Paperclip } from '@openai/apps-sdk-ui/components/Icon'
import { PROVIDER_INFO } from '../ChatView/providerRegistry.jsx'
import EffortStepper from '../ui/EffortStepper.jsx'
import ActivityLineHeader from '../ChatView/ActivityLineHeader.jsx'
import SourceFavicon from '../ChatView/SourceFavicon.jsx'
import GoalPlanDetails from '../ChatView/GoalPlanDetails.jsx'
import LifecycleIcon from '../ChatView/LifecycleIcon.jsx'
import QuestionCard from '../ChatView/QuestionCard.jsx'
import '../ChatView/ChatView.css'
import '../ChatView/QuestionCard.css'
import { Typewriter, useLoopingTimeline, usePrefersReducedMotion } from './WalkthroughMotion.jsx'
import { Touch, fingerPath } from './WalkthroughTouch.jsx'

const CHAT_PROMPT = 'Build me an app to track my expenses'
// blank · tap the brain · picker opens (the finger waits) · finger glides to the row · tap the row · chosen · typing · thinking · building · done (ms each).
// One finger runs through phases 1 to 5: it arrives at the brain and presses, waits while the picker opens, glides to the row, presses, and fades.
const CHAT_PHASES = [400, 1100, 450, 450, 450, 500, 2400, 1500, 2000, 4200]
const PHASE = { blank: 0, tapBrain: 1, open: 2, aim: 3, tapRow: 4, chosen: 5, typing: 6, thinking: 7, building: 8, done: 9 }
// Rows as the real picker lists them (connected providers in order, Codex then Claude); Sonnet 5.5 gets chosen.
const PICKER_MODELS = [
  { name: 'GPT-6-Astra', provider: 'codex' },
  { name: 'Claude Sonnet 5.5', provider: 'claude', chosen: true },
]
const CLAUDE_EFFORTS = PROVIDER_INFO.claude.efforts
const EXPENSE_BARS = [38, 62, 46, 80, 54]
const EXPENSE_ROWS = [['Groceries', '$84.20'], ['Train pass', '$46.00']]

function ExpensesApp() {
  return <div className="wt-app" aria-hidden="true">
    <div className="wt-app__total"><small>Spent this month</small><b>$1,284.50</b></div>
    <div className="wt-app__bars">{EXPENSE_BARS.map((height, index) => <i key={index} style={{ '--h': `${height}%`, '--i': index }} />)}</div>
    <ul className="wt-app__rows">{EXPENSE_ROWS.map(([name, amount], index) => <li key={name} style={{ '--i': index }}><span>{name}</span><span>{amount}</span></li>)}</ul>
  </div>
}

/* The Möbius chat as a new owner meets it: no model yet, so pick one from the
   brain button, then ask for an app. When the agent finishes, Möbius opens the
   app in a pane beside the conversation by itself, as it does for real. */
export function AgentChatDemo() {
  const phase = useLoopingTimeline(CHAT_PHASES)
  const modelChosen = phase >= PHASE.chosen
  const pickerOpen = phase >= PHASE.open && phase <= PHASE.chosen
  const chatRef = useRef(null)
  const brainRef = useRef(null)
  const rowRef = useRef(null)
  const [brainPos, setBrainPos] = useState(null)
  const [rowPos, setRowPos] = useState(null)

  // The brain is measured before the finger appears, and the row once the picker is open, so the
  // finger never starts from a stale or default position.
  useLayoutEffect(() => {
    const chat = chatRef.current
    if (!chat) return
    if (phase === PHASE.blank && brainRef.current) setBrainPos(fingerPath(brainRef.current, chat))
    else if (phase === PHASE.open && rowRef.current) setRowPos(fingerPath(rowRef.current, chat))
  }, [phase])
  // The finger glides to the row as soon as the picker is open, lands, and only then presses (tapRow);
  // the row is selected after that press (chosen).
  const finger = phase >= PHASE.aim ? rowPos || brainPos : brainPos

  return <div className="wt-chatmock" role="img" aria-label="Looping demo of the Möbius chat. You pick a model from the brain button, ask for an expense tracker, the agent builds it, and the finished app opens beside the conversation.">
    <div className="wt-chatmock__chat" ref={chatRef} aria-hidden="true">
      <div className="wt-chatmock__log">
        {phase >= PHASE.thinking && <div className="wt-user">{CHAT_PROMPT}</div>}
        {phase >= PHASE.thinking && <div className="chat__tools wt-steps">
          {phase === PHASE.thinking && <div className="chat__activity chat__activity--running"><ActivityLineHeader text="Thinking" displayState="running" iconKind="reasoning" /></div>}
          {phase >= PHASE.building && <div className="chat__activity"><ActivityLineHeader text="Thought for 2 seconds" displayState="done" iconKind="reasoning" /></div>}
          {phase === PHASE.building && <div className="chat__activity chat__activity--running"><ActivityLineHeader text="Building Expenses" displayState="running" iconKind="terminal" /></div>}
        </div>}
        {phase >= PHASE.done && <div className="chat__text chat__text--assistant wt-native">I built Expenses and opened it beside our chat. Log your spending and see where it goes.</div>}
        {!modelChosen && !pickerOpen && <p className="wt-chatmock__hint">Pick a model to start chatting.</p>}
      </div>
      {pickerOpen && <div className="wt-picker">
        <div className="wt-picker__attach">
          <span className="wt-picker__logo"><Paperclip width={18} height={18} /></span>
          <span className="wt-picker__text"><strong>Attach files</strong><small>Images, PDFs, code</small></span>
        </div>
        <div className="wt-picker__heading">
          <div className="wt-picker__label">Model</div>
          <div className="wt-picker__usage"><span><i className="is-provider" />Weekly usage</span><span><i className="is-context" />Context</span></div>
        </div>
        {PICKER_MODELS.map(({ name, provider, chosen }) => {
          const { Logo, label } = PROVIDER_INFO[provider]
          const selected = Boolean(chosen) && modelChosen
          return <div key={name}>
            <div ref={chosen ? rowRef : undefined} className={`wt-picker__row${selected ? ' is-chosen' : ''}${chosen && phase === PHASE.tapRow ? ' is-aimed' : ''}`}>
              <span className={`wt-picker__logo is-${provider}`}><Logo /></span>
              <span className="wt-picker__text"><strong>{name}</strong><small>{label}</small></span>
              <span className="wt-picker__radio" />
            </div>
            {selected && <div className="wt-picker__effort"><EffortStepper efforts={CLAUDE_EFFORTS} value="medium" onChange={() => {}} disabled /></div>}
          </div>
        })}
      </div>}
      <div className="wt-chatmock__composer">
        <span ref={brainRef} className={`wt-brainbtn${pickerOpen ? ' is-open' : ''}`}><BrainUsageIcon leftPercent={modelChosen ? 14 : null} rightPercent={modelChosen ? 6 : null} width={34} height={34} /></span>
        <span className={`wt-pill${phase === PHASE.typing ? ' is-focused' : ''}`}>
          <span className="wt-pill__text">{phase === PHASE.typing ? <Typewriter key="type" text={CHAT_PROMPT} speed={42} startDelay={120} /> : <span className="wt-pill__placeholder">{modelChosen ? 'Message Möbius…' : 'Choose a model to start…'}</span>}</span>
          {phase === PHASE.typing ? <span className="wt-send is-armed">↑</span> : MIC}
        </span>
      </div>
      {phase >= PHASE.tapBrain && phase <= PHASE.chosen && finger && <Touch x={finger.x} y={finger.y} from={finger.from} second={phase >= PHASE.tapRow} out={phase === PHASE.chosen} />}
    </div>
    <div className="wt-chatmock__pane" aria-hidden="true">
      {phase >= PHASE.done
        ? <><div className="wt-chatmock__title"><span className="wt-chatmock__icon">E</span><strong>Expenses</strong><small>October</small></div><ExpensesApp /></>
        : <p className="wt-chatmock__empty">Apps open here.</p>}
    </div>
  </div>
}

const SOURCES = [
  { host: 'japan-guide.com', title: 'Kyoto cherry blossom forecast' },
  { host: 'kyoto.travel', title: 'Autumn leaves in Kyoto' },
  { host: 'jnto.go.jp', title: 'When to visit Japan' },
]
// A real goal plan, rendered by the same GoalPlanDetails the chat uses. It is shown working, as in the
// chat: each step runs and completes in turn, and the Goal ends as completed.
const GOAL_STEPS = ['Find flights and a hotel', 'Plan each day', 'Save the itinerary to Pages']
const goalPlan = stage => ({ tasks: GOAL_STEPS.map((title, index) => ({
  id: `step-${index}`,
  title,
  status: stage > index ? 'completed' : stage === index ? 'running' : 'pending',
  waiting_on: stage < index ? [`step-${index - 1}`] : [],
})) })
const BUY_QUESTION = [{ question: 'Which one should I order?', options: [
  { label: 'Aero Q45 · $79', description: 'Best match, arrives Tuesday' },
  { label: 'Nimbus ANC · $64', description: 'Cheapest, arrives Thursday' },
] }]

/* What each request leaves behind. These are the pieces a real chat shows, using
   Möbius' own components and classes so type, spacing and shape match the app. */

/* The agent stops and asks. This is the real question card. A finger taps the first option, the
   answer goes in, and the agent carries on and confirms the order. It plays by itself: the card is
   not tappable here (the Submit button is hidden for the same reason). */
function BuyResult({ onFinger }) {
  const reduced = usePrefersReducedMotion()
  const [picked, setPicked] = useState(null)
  const wrapRef = useRef(null)
  const replyRef = useRef(null)
  useEffect(() => { if (picked) replyRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }) }, [picked])
  useEffect(() => {
    const wrap = wrapRef.current
    const option = () => wrap?.querySelectorAll('.qcard__opt')[0]
    const timers = []
    const at = (ms, fn) => timers.push(setTimeout(fn, ms))
    if (reduced) { at(0, () => option()?.click()); at(450, () => wrap?.querySelector('.qcard__submit:not(:disabled)')?.click()) } else {
      // The finger lives in the frame around the chat (the log clips anything that sticks out of it).
      at(700, () => {
        const target = option()
        const frame = wrap?.closest('.wt-chatflow-wrap')
        const log = wrap?.closest('.wt-chatflow__log')
        if (!target || !frame || !log) return
        const spot = fingerPath(target, frame)
        onFinger({ ...spot, y: spot.y - log.scrollTop, out: false })
      })
      at(1400, () => option()?.click())
      at(1950, () => { onFinger(value => value && { ...value, out: true }); wrap?.querySelector('.qcard__submit:not(:disabled)')?.click() })
    }
    return () => { timers.forEach(clearTimeout); onFinger(null) }
  }, [reduced, onFinger])
  const onAnswer = (_text, resolved) => {
    const label = Object.values(resolved)[0]
    setPicked(BUY_QUESTION[0].options.find(option => option.label === label) || { label, description: '' })
    return Promise.resolve(true)
  }
  return <div ref={wrapRef} className="wt-native" aria-label="Result: a question asking which headphones to order. The first option gets picked.">
    <QuestionCard chatId="walkthrough" questionId="walkthrough-buy" questions={BUY_QUESTION} onAnswer={onAnswer} disabled={false} />
    {picked && <div ref={replyRef} className="chat__text chat__text--assistant wt-native">I ordered the {picked.label.split(' · ')[0]} for {picked.label.split(' · ')[1]}. It {picked.description.split(', ')[1] || 'is on its way'}.</div>}
  </div>
}

/* A plain answer, with its References folded away until you open them. */
function LookupResult() {
  const [open, setOpen] = useState(false)
  return <div className="wt-native" aria-label="Result: a short answer with its references.">
    <div className="chat__text chat__text--assistant">Go in late March to early April for cherry blossoms, or mid November for autumn leaves. Book about a month ahead, because both seasons fill up fast.</div>
    <section className={`chat__sources${open ? ' chat__sources--open' : ''}`}>
      <button type="button" className="chat__sources-toggle" aria-expanded={open} onClick={() => setOpen(value => !value)}>
        <span className="chat__sources-label">References</span><span className="chat__sources-count">3</span>
        <ChevronDown className="chat__sources-chevron" width={16} height={16} aria-hidden="true" />
      </button>
      <div className="chat__sources-body" hidden={!open}>
        <ul className="chat__sources-list">{SOURCES.map(({ host, title }) => <li key={host} className="chat__source-item chat__source-item--web"><span className="chat__source-chip"><SourceFavicon faviconUrl="" fallback={host[0].toUpperCase()} /><span className="chat__source-title">{title}</span></span></li>)}</ul>
      </div>
    </section>
  </div>
}

/* A Goal: the plan with each step's status, updating as the agent works through it, ending completed. */
function HelpResult() {
  const reduced = usePrefersReducedMotion()
  const [stage, setStage] = useState(reduced ? GOAL_STEPS.length : 0)
  useEffect(() => {
    if (reduced) return undefined
    const timer = setInterval(() => setStage(value => Math.min(value + 1, GOAL_STEPS.length)), 1100)
    return () => clearInterval(timer)
  }, [reduced])
  const completed = stage === GOAL_STEPS.length
  return <aside className={`chat__goal-history wt-native${completed ? ' chat__goal-history--completed' : ''}`} style={completed ? undefined : { '--lifecycle-tone': 'var(--accent)' }} aria-label={completed ? 'Result: a completed goal with all three steps done.' : 'Result: a goal the agent is working through.'}>
    <LifecycleIcon kind="goal" />
    <div className="chat__goal-history-copy">
      <span className="chat__goal-history-kicker">{completed ? 'Goal completed' : 'Goal'}</span>
      <strong className="chat__goal-history-objective">Plan a weekend trip to Lisbon</strong>
      <span className="chat__goal-history-meta">{stage} of 3 steps</span>
      <GoalPlanDetails plan={goalPlan(stage)} />
    </div>
  </aside>
}

const REQUESTS = [
  { id: 'buy', summary: 'The agent asks which headphones to order. It picks the Aero Q45 for $79 and confirms the order.', short: 'Buy something', label: 'Buy me headphones under $100', tools: [['search', 'Searched stores'], ['web', 'Compared prices'], ['plan', 'Asked which to order']], Result: BuyResult },
  { id: 'look', summary: 'The agent answers: go in late March to early April for cherry blossoms, or mid November for autumn leaves, and book about a month ahead. It cites three references.', short: 'Look something up', label: 'Look up the best time to visit Kyoto', tools: [['web', 'Searched the web'], ['files', 'Read 3 pages'], ['edit', 'Wrote the answer']], Result: LookupResult },
  { id: 'help', summary: 'The agent works through a goal to plan a weekend trip to Lisbon: find flights and a hotel, plan each day, and save the itinerary. The goal completes.', short: 'Plan a big task', label: 'Plan a weekend trip to Lisbon', tools: [['search', 'Looked up flights and hotels'], ['plan', 'Planned the days'], ['edit', 'Saved the itinerary']], Result: HelpResult },
]
// Time before each stage, in ms: the request is typed, sent, the agent starts thinking, its tool steps, then the result.
const TYPE_START = 500
const STEP_DELAYS = [TYPE_START + 1500, 450, 1000, 1100]
const newRun = (key, reduced) => ({ key, shown: reduced ? STEP_DELAYS.length : 0, typed: false, finger: null })
const MIC = <span className="wt-pill__action" aria-hidden="true"><svg className="wt-mic" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg></span>

/* One real-looking Möbius chat. Tap a suggestion and it is typed and sent from the composer, then
   the conversation unfolds the way it does for real: your message, a Thinking line once the agent
   starts, its tool steps, and finally a result you can use. Tap again to replay. */
export function AgentBrainFlow() {
  const reduced = usePrefersReducedMotion()
  const logRef = useRef(null)
  const [selected, setSelected] = useState(0)
  const [run, setRun] = useState(0)
  // Everything that belongs to one run (how far it got, the typing pause, the finger) is stored under
  // that run's key. Switching tabs or replaying therefore starts from the true initial state in the
  // very same render, with no frame of the previous run's leftovers.
  const runKey = `${selected}-${run}`
  const [progress, setProgress] = useState(() => newRun(runKey, reduced))
  const current = progress.key === runKey ? progress : newRun(runKey, reduced)
  const { shown, typed: typeStarted, finger } = current
  const patch = useCallback(change => setProgress(previous => {
    const base = previous.key === runKey ? previous : newRun(runKey, reduced)
    return { ...base, ...change(base) }
  }), [runKey, reduced])
  const setFinger = useCallback(value => patch(base => ({ finger: typeof value === 'function' ? value(base.finger) : value })), [patch])
  useEffect(() => {
    if (reduced) return undefined
    let count = 0
    let timer
    const advance = () => {
      timer = setTimeout(() => {
        count += 1
        patch(() => ({ shown: count }))
        if (count < STEP_DELAYS.length) advance()
      }, STEP_DELAYS[count])
    }
    advance()
    // The composer rests on its placeholder for a beat before the request starts typing.
    const typingTimer = setTimeout(() => patch(() => ({ typed: true })), TYPE_START)
    return () => { clearTimeout(timer); clearTimeout(typingTimer) }
  }, [runKey, reduced, patch])
  // Like a real chat, the newest message stays in view.
  useEffect(() => {
    const log = logRef.current
    if (log) log.scrollTo({ top: log.scrollHeight, behavior: reduced ? 'auto' : 'smooth' })
  }, [shown, runKey, reduced])
  const request = REQUESTS[selected]
  const choose = index => { if (index === selected) setRun(value => value + 1); else setSelected(index) }
  const done = shown > 3
  const typing = shown === 0 && !reduced
  return <div className="wt-chatflow-wrap">
    <div className="wt-chatflow">
      <div className="wt-chatflow__chips" role="group" aria-label="Things you can ask">
        {REQUESTS.map((item, index) => <button key={item.id} type="button" className={`wt-chatflow__tab${index === selected ? ' is-active' : ''}`} aria-pressed={index === selected} onClick={() => choose(index)}>{item.short}</button>)}
      </div>
      <div className="wt-chatflow__log" ref={logRef} role="log" aria-label={`Chat: ${request.label}`}>
        {shown > 0 && <div className="wt-user">{request.label}</div>}
        {shown > 1 && <div className="chat__tools wt-steps">
          {shown < 3 && <div className="chat__activity chat__activity--running"><ActivityLineHeader text="Thinking" displayState="running" iconKind="reasoning" ariaLabel="Thinking, in progress" /></div>}
          {shown === 3 && <div className="chat__activity-timeline">{request.tools.map(([kind, text], index) => <div key={text} className="chat__activity wt-steps__row" style={{ '--i': index }}><ActivityLineHeader text={text} displayState="done" iconKind={kind} /></div>)}</div>}
          {done && <div className="chat__activity"><ActivityLineHeader text={`Worked through ${request.tools.length} steps`} displayState="done" iconKind="sparkle" /></div>}
        </div>}
        {done && <div className="wt-chatflow__reply" inert><request.Result key={`${selected}-${run}`} onFinger={setFinger} /></div>}
      </div>
      <p className="sr-only" role="status">{done ? request.summary : ''}</p>
      <div className="wt-chatmock__composer">
        <span className="wt-brainbtn"><BrainUsageIcon leftPercent={14} rightPercent={6} width={34} height={34} /></span>
        <span className={`wt-pill${typing && typeStarted ? ' is-focused' : ''}`}>
          <span className="wt-pill__text">{typing && typeStarted
            ? <Typewriter key={`${selected}-${run}`} text={request.label} speed={32} startDelay={0} />
            : <span className="wt-pill__placeholder">Message Möbius…</span>}</span>
          {typing && typeStarted ? <span className="wt-send is-armed">↑</span> : MIC}
        </span>
      </div>
    </div>
    {finger && <Touch x={finger.x} y={finger.y} from={finger.from} second={false} out={finger.out} />}
  </div>
}
