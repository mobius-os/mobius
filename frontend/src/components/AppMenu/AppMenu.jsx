import { useEffect, useRef, useState } from 'react'
import './AppMenu.css'

// Three-dots menu that appears in the shell header when the user is
// viewing a mini-app. v1 has one action: "Add to home screen", which
// opens the app's standalone surface at /apps/<slug>/ in a new tab.
// That standalone page is responsible for the actual install affordance
// (it captures `beforeinstallprompt` and renders an install button, or
// shows iOS-specific instructions). Keeping the install logic on the
// standalone page rather than here means Möbius doesn't need to track
// per-app `beforeinstallprompt` events — the browser only fires that
// for the currently-displayed manifest, which is always Möbius's own
// manifest while the user is inside the shell.
//
// The popover positions itself absolutely to the right of the trigger
// button. Closes on: outside-click, Escape, item click.
export default function AppMenu({ app }) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef(null)
  const menuRef = useRef(null)

  useEffect(() => {
    if (!open) return
    function onPointer(e) {
      if (
        menuRef.current?.contains(e.target) ||
        triggerRef.current?.contains(e.target)
      ) return
      setOpen(false)
    }
    function onKey(e) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!app || !app.slug) return null

  function addToHomeScreen() {
    setOpen(false)
    // Same-tab navigation rather than window.open — popups inside an
    // installed standalone PWA get redirected to the default browser
    // and break the install flow. A same-tab navigation stays in the
    // PWA context; the user can return via the OS back-gesture or the
    // Edit pill in the standalone app.
    window.location.href = `/apps/${app.slug}/`
  }

  return (
    <div className="app-menu">
      <button
        ref={triggerRef}
        className="app-menu__trigger"
        aria-label="App actions"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      {open && (
        <div ref={menuRef} className="app-menu__popover" role="menu">
          <button
            className="app-menu__item"
            role="menuitem"
            onClick={addToHomeScreen}
          >
            <span className="app-menu__item-icon" aria-hidden="true">📲</span>
            <span>Add to home screen</span>
          </button>
        </div>
      )}
    </div>
  )
}
