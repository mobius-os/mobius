import { Component } from 'react'
import './ErrorBoundary.css'

/**
 * App-level error boundary. Without one, a render throw anywhere below
 * white-screens the entire PWA — acute here because the host renders
 * agent-generated, breakable markdown (marked + KaTeX/hljs injected via
 * dangerouslySetInnerHTML), so one malformed token takes the whole tree
 * down. Catching it keeps the crash recoverable and DIAGNOSABLE, in the
 * spirit of the recovery-over-prevention model — a broken state must leave
 * a trace, not vanish into a white screen.
 *
 * Props:
 *   children  — the subtree to guard
 *   label     — names the guarded surface in the crash record / console
 *   onReset   — optional; called on "Try again" so the caller can clear
 *               related state before the subtree re-mounts
 *   variant   — 'fullscreen' (default) covers the viewport; 'inline' fills
 *               the nearest positioned ancestor, so a guarded view can fail
 *               without taking the surrounding chrome (drawer/nav) down
 */
export default class ErrorBoundary extends Component {
  state = { error: null }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    const where = this.props.label || 'app'
    const componentStack = info?.componentStack || ''
    // Console for live debugging, plus a small persisted record the recovery
    // surface can show ("the shell last crashed with X"). This is the
    // minimal version of the missing client-error telemetry; a real sink can
    // hook the same spot later.
    console.error(`[ErrorBoundary:${where}]`, error, componentStack)
    try {
      sessionStorage.setItem(
        'mobius:last-error',
        JSON.stringify({
          where,
          message: String(error?.message || error),
          stack: String(error?.stack || '').slice(0, 2000),
          componentStack: componentStack.slice(0, 2000),
          at: new Date().toISOString(),
        }),
      )
    } catch {
      /* storage full/disabled — the console line above still stands */
    }
  }

  handleRetry = () => {
    this.props.onReset?.()
    this.setState({ error: null })
  }

  handleReload = () => {
    // Mirror App.jsx's shell-reload path so the reload skips the splash.
    try {
      sessionStorage.setItem('shell-reload', '1')
    } catch {
      /* ignore */
    }
    window.location.reload()
  }

  render() {
    if (!this.state.error) return this.props.children
    const message = String(this.state.error?.message || this.state.error)
    const cls = this.props.variant === 'inline' ? 'errbound errbound--inline' : 'errbound'
    return (
      <div className={cls} role="alert" aria-live="assertive">
        <div className="errbound__card">
          <h1 className="errbound__title">Something broke</h1>
          <p className="errbound__body">
            This screen hit an unexpected error. Your chats and data are safe —
            you can retry, or reload the app.
          </p>
          <pre className="errbound__detail">{message}</pre>
          <div className="errbound__actions">
            <button type="button" className="errbound__btn" onClick={this.handleRetry}>
              Try again
            </button>
            <button
              type="button"
              className="errbound__btn errbound__btn--primary"
              onClick={this.handleReload}
            >
              Reload app
            </button>
          </div>
        </div>
      </div>
    )
  }
}
