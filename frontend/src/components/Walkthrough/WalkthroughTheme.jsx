/* A looping explainer for the "Shape Möbius" screen: ask the agent for a new theme in the same
   chat as before, and a small Möbius beside it recolors itself. It uses the guide's own chat
   pieces, so the conversation looks like the real one. */
import BrainUsageIcon from '../ChatView/BrainUsageIcon.jsx'
import ActivityLineHeader from '../ChatView/ActivityLineHeader.jsx'
import '../ChatView/ChatView.css'
import { Typewriter, useLoopingTimeline } from './WalkthroughMotion.jsx'
import { ComposerAction, MobiusMenu } from './WalkthroughChrome.jsx'

const THEME_PROMPT = 'Give Möbius a bold Halloween theme.'
// blank · typing · thinking · editing · done (ms each). The theme changes as the edit finishes.
const THEME_PHASES = [600, 2600, 1300, 1700, 4800]
const PHASE = { blank: 0, typing: 1, thinking: 2, editing: 3, done: 4 }

/* Möbius in miniature, as one screen: a menu beside the conversation. The whole screen takes its
   colors from the theme, so when the agent finishes the edit it repaints with the chat still in it. */
export function ThemeChangeDemo() {
  const phase = useLoopingTimeline(THEME_PHASES)
  const typing = phase === PHASE.typing
  const sent = phase >= PHASE.thinking
  const themed = phase >= PHASE.done
  return <div className={`wt-chatmock wt-themed${themed ? ' is-themed' : ''}`} role="img" aria-label="Looping demo of the Möbius chat. You ask for a bold Halloween theme, the agent edits Möbius, and the whole screen changes: the logo becomes a pumpkin and the colors turn deep purple and orange.">
    <div className="wt-themed__sky" aria-hidden="true">
      <i className="wt-themed__mist wt-themed__mist--one" /><i className="wt-themed__mist wt-themed__mist--two" />
      <i className="wt-themed__moon" />
      <img className="wt-themed__witch wt-themed__witch--one" src="/walkthrough/halloween-witch.webp" alt="" draggable={false} />
      <img className="wt-themed__witch wt-themed__witch--two" src="/walkthrough/halloween-witch.webp" alt="" draggable={false} />
      <img className="wt-themed__witch wt-themed__witch--anchor" src="/walkthrough/halloween-witch.webp" alt="" draggable={false} />
    </div>
    <MobiusMenu />
    <div className="wt-chatmock__chat" aria-hidden="true">
      <div className="wt-chatmock__log">
        {!sent && <div className="wt-landing">
          <img className="chat__empty-glyph" src="/moebius.png" alt="" width="52" height="52" draggable={false} />
          <p className="chat__empty-title">What&apos;s on your mind?</p>
        </div>}
        {sent && <div className="wt-user">{THEME_PROMPT}</div>}
        {sent && <div className="chat__tools wt-steps">
          {phase === PHASE.thinking && <div className="chat__activity chat__activity--running"><ActivityLineHeader text="Thinking" displayState="running" iconKind="reasoning" /></div>}
          {phase >= PHASE.editing && <div className="chat__activity"><ActivityLineHeader text="Thought for 2 seconds" displayState="done" iconKind="reasoning" /></div>}
          {phase >= PHASE.editing && <div className="chat__activity"><ActivityLineHeader text="Read theme.css" displayState="done" iconKind="files" /></div>}
          {phase === PHASE.editing && <div className="chat__activity chat__activity--running"><ActivityLineHeader text="Editing the theme" displayState="running" iconKind="edit" /></div>}
          {phase >= PHASE.done && <div className="chat__activity"><ActivityLineHeader text="Edited the theme" displayState="done" iconKind="edit" /></div>}
        </div>}
        {themed && <div className="chat__text chat__text--assistant wt-native">Done! Möbius now has a bold Halloween theme. Ask me any time to change it back.</div>}
      </div>
      <div className="wt-chatmock__composer">
        <span className="wt-brainbtn"><BrainUsageIcon leftPercent={14} rightPercent={6} width={34} height={34} /></span>
        <span className={`wt-pill${typing ? ' is-focused' : ''}`}>
          <span className="wt-pill__text">{typing ? <Typewriter key="theme-type" text={THEME_PROMPT} speed={60} startDelay={150} reserve={false} placeholder="Message Möbius…" /> : <span className="wt-pill__placeholder">Message Möbius…</span>}</span>
          <ComposerAction kind={typing ? 'send' : phase >= PHASE.thinking && phase < PHASE.done ? 'stop' : 'mic'} />
        </span>
      </div>
    </div>
  </div>
}
