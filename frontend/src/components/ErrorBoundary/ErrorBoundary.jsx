import { Component } from 'react'
import { api, BASE } from '../../api/client.js'
import { recordClientError } from '../../lib/errorLog.js'
import {
  buildAgentRepairPrompt,
  clearErrorRecoveryAttempt,
  ERROR_RECOVERY_STABLE_MS,
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
  recoveryPhaseForAttempt,
  repairChatPath,
  startAgentRepair,
  writeErrorRecoveryAttempt,
} from '../../lib/errorRecovery.js'
import RecoveryLink from './RecoveryLink.jsx'
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
 *   onReset   — optional; called before a refresh so the caller can notify
 *               an owning surface that its child failed
 *   variant   — 'fullscreen' (default) covers the viewport; 'inline' fills
 *               the nearest positioned ancestor, so a guarded view can fail
 *               without taking the surrounding chrome (drawer/nav) down
 *   recoveryKey — stable identity for the guarded resource; prevents a healthy
 *                 retained pane from clearing another pane's recovery attempt
 *   canAskAgent — false for restricted surfaces that cannot create owner chats
 */
export default class ErrorBoundary extends Component {
  state = {
    error: null,
    phase: 'refresh',
    agentError: '',
    repairChatId: null,
  }

  stableTimer = null
  crashContext = null
  agentStarting = false

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    clearTimeout(this.stableTimer)
    // Record through the shared client-error log (console + ring buffer the
    // recovery surface can read), same sink the global window handlers use.
    recordClientError({
      where: this.props.label || 'app',
      message: error?.message || error,
      error,
      componentStack: info?.componentStack,
    })
    const message = String(error?.message || error)
    const surfaceKey = this.surfaceKey()
    const fingerprint = errorRecoveryFingerprint(surfaceKey, message)
    const attempt = readErrorRecoveryAttempt({ surfaceKey, fingerprint })
    this.crashContext = {
      surfaceKey,
      fingerprint,
      message,
      componentStack: info?.componentStack || '',
    }
    this.setState({
      phase: recoveryPhaseForAttempt(attempt, {
        canAskAgent: this.props.canAskAgent !== false,
      }),
      agentError: attempt?.phase === 'agent-failed'
        ? 'Möbius couldn’t start the repair chat.'
        : '',
      repairChatId: attempt?.chatId || null,
    })
  }

  componentDidMount() {
    this.armStableClear()
  }

  componentWillUnmount() {
    clearTimeout(this.stableTimer)
  }

  surfaceKey = () => this.props.recoveryKey || this.props.label || 'app'

  armStableClear = () => {
    clearTimeout(this.stableTimer)
    this.stableTimer = setTimeout(() => {
      if (!this.state.error) clearErrorRecoveryAttempt(this.surfaceKey())
    }, ERROR_RECOVERY_STABLE_MS)
  }

  handleRefresh = () => {
    const context = this.crashContext
    if (context) {
      writeErrorRecoveryAttempt({
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
        phase: 'refreshed',
      })
    }
    this.props.onReset?.()
    // Mirror App.jsx's shell-reload path so a full refresh skips the splash.
    try {
      sessionStorage.setItem('shell-reload', '1')
    } catch {
      /* ignore */
    }
    window.location.reload()
  }

  handleAgentRepair = async () => {
    const context = this.crashContext
    if (!context || this.agentStarting) return
    this.agentStarting = true
    this.setState({ phase: 'agent-starting', agentError: '' })
    try {
      const result = await startAgentRepair({
        client: api,
        base: BASE,
        prompt: buildAgentRepairPrompt({
          surface: context.surfaceKey,
          message: context.message,
          componentStack: context.componentStack,
          pathname: window.location.pathname,
        }),
      })
      writeErrorRecoveryAttempt({
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
        phase: 'agent-directed',
        chatId: result.chatId,
      })
      window.location.assign(result.path)
    } catch {
      this.agentStarting = false
      writeErrorRecoveryAttempt({
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
        phase: 'agent-failed',
      })
      this.setState({
        phase: 'recovery',
        agentError: 'Möbius couldn’t start the repair chat.',
      })
    }
  }

  openRepairChat = () => {
    if (this.state.repairChatId) {
      window.location.assign(repairChatPath(this.state.repairChatId, BASE))
    }
  }

  render() {
    if (!this.state.error) return this.props.children
    const message = String(this.state.error?.message || this.state.error)
    const cls = this.props.variant === 'inline' ? 'errbound errbound--inline' : 'errbound'
    const { phase } = this.state
    const afterRefresh = phase !== 'refresh'
    const recovery = phase === 'recovery'
    return (
      <div className={cls} role="alert" aria-live="assertive">
        <div className="errbound__card">
          <h1 className="errbound__title">Something broke</h1>
          <p className="errbound__body">
            {phase === 'refresh' && (
              <>This screen hit an unexpected error. Your chats and data are safe. Refresh the screen to try it again.</>
            )}
            {(phase === 'agent' || phase === 'agent-starting') && (
              <>Refreshing didn’t fix this screen. Möbius can send the error to a new repair chat for your agent to investigate.</>
            )}
            {recovery && this.state.repairChatId && (
              <>The repair chat started, but this screen still can’t open. System recovery is the remaining fallback.</>
            )}
            {recovery && !this.state.repairChatId && (
              <>The repair chat couldn’t start. You can try the agent again or use system recovery as a last resort.</>
            )}
          </p>
          <pre className="errbound__detail">{message}</pre>
          {this.state.agentError && (
            <p className="errbound__status" role="status">{this.state.agentError}</p>
          )}
          <div className="errbound__actions">
            {afterRefresh && (
              <button type="button" className="errbound__btn" onClick={this.handleRefresh}>
                Refresh again
              </button>
            )}
            {recovery && this.state.repairChatId && (
              <button type="button" className="errbound__btn" onClick={this.openRepairChat}>
                Open repair chat
              </button>
            )}
            {!(recovery && this.state.repairChatId) && (
              <button
                type="button"
                className="errbound__btn errbound__btn--primary"
                onClick={phase === 'refresh' ? this.handleRefresh : this.handleAgentRepair}
                disabled={phase === 'agent-starting'}
              >
                {phase === 'refresh' && 'Refresh screen'}
                {phase === 'agent' && 'Ask agent to fix'}
                {phase === 'agent-starting' && 'Starting repair chat…'}
                {recovery && 'Try agent again'}
              </button>
            )}
          </div>
          {recovery && (
            <RecoveryLink lead="If the repair chat can’t get you back in," />
          )}
        </div>
      </div>
    )
  }
}
