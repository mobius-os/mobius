/* Pieces of the real Möbius that several guide previews copy, built from the real classes and icons
   so they cannot drift from the app: the composer's primary button and the menu. */
import { PrimaryActionGlyphs } from '../ChatView/ChatInputBar.jsx'
import ComposerMicIcon from '../ChatView/ComposerMicIcon.jsx'
import { AppsNavIcon, NewChatNavIcon, ProjectsNavIcon, SearchNavIcon, SettingsNavIcon } from '../navigationIcons.js'

/* The composer's round button as the real chat shows it: a microphone when idle, an arrow once there
   is something to send, and the red stop square while the agent is working. Decorative here. */
export function ComposerAction({ kind }) {
  if (kind === 'mic') return <span className="chat__action chat__mic" aria-hidden="true"><ComposerMicIcon /></span>
  return <span className={`chat__action ${kind === 'stop' ? 'chat__stop' : 'chat__send'}`} aria-hidden="true">
    <PrimaryActionGlyphs action={kind} />
  </span>
}

/* The Möbius menu as it looks: logo and name with search, New chat, Apps, Projects and Settings. The
   logo becomes a pumpkin when the Halloween theme is on. Nothing in it is clickable. */
export function MobiusMenu() {
  return <nav className="wt-menu" aria-hidden="true">
    <div className="wt-menu__brand">
      <img className="shell__logo" src="/moebius.png" alt="" width="28" height="28" draggable={false} />
      <img className="wt-menu__pumpkin" src="/walkthrough/halloween-mobius.svg" alt="" width="28" height="28" draggable={false} />
      <span className="shell__wordmark">Möbius</span>
      <span className="wt-menu__search"><SearchNavIcon width={20} height={20} /></span>
    </div>
    <div className="drawer__item drawer__item--new"><span className="drawer__item-icon"><NewChatNavIcon /></span><span className="drawer__item-text">New chat</span></div>
    <div className="drawer__item drawer__item--apps"><span className="drawer__item-icon"><AppsNavIcon /></span><span className="drawer__item-text">Apps</span></div>
    <div className="drawer__item drawer__item--projects"><span className="drawer__item-icon"><ProjectsNavIcon /></span><span className="drawer__item-text">Projects</span></div>
    <div className="wt-menu__foot drawer__item"><span className="drawer__item-icon"><SettingsNavIcon /></span><span className="drawer__item-text">Settings</span></div>
  </nav>
}
