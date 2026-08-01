import { Component } from 'react'
import { api, BASE } from '../../api/client.js'
import { recordClientError } from '../../lib/errorLog.js'
import {
  buildAgentRepairPrompt,
  createRepairIdentity,
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
  redactDiagnosticText,
  recoveryActionPolicy,
  recoveryPhaseForAttempt,
  repairChatPath,
  startAgentRepair,
  writeErrorRecoveryAttempt,
  writeRefreshedRecoveryAttempt,
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
    phase: 'resolving',
    agentError: '',
    repairChatId: null,
    attemptPhase: null,
  }

  crashContext = null
  agentStarting = false
  repairController = null
  pageShowListening = false
  headingRef = null

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
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
    const componentStack = info?.componentStack || ''
    const fingerprint = errorRecoveryFingerprint(surfaceKey, message, componentStack)
    const attempt = readErrorRecoveryAttempt({ surfaceKey, fingerprint })
    this.crashContext = {
      surfaceKey,
      fingerprint,
      message,
      componentStack,
      attempt,
    }
    this.listenForPageShow()
    this.setState({
      phase: recoveryPhaseForAttempt(attempt, {
        canAskAgent: this.props.canAskAgent !== false,
      }),
      agentError: attempt?.phase === 'agent-failed'
        ? 'Repair chat request failed.'
        : '',
      repairChatId: attempt?.chatId || null,
      attemptPhase: attempt?.phase || null,
    })
  }

  componentDidUpdate(_prevProps, prevState) {
    if (prevState.phase === 'resolving' && this.state.phase !== 'resolving') {
      this.headingRef?.focus()
    }
  }

  componentWillUnmount() {
    this.repairController?.abort()
    if (this.pageShowListening) window.removeEventListener('pageshow', this.handlePageShow)
  }

  surfaceKey = () => this.props.recoveryKey || this.props.label || 'app'

  listenForPageShow = () => {
    if (this.pageShowListening) return
    window.addEventListener('pageshow', this.handlePageShow)
    this.pageShowListening = true
  }

  handlePageShow = (event) => {
    const context = this.crashContext
    if (!event.persisted || !context) return
    this.agentStarting = false
    this.repairController = null
    const attempt = readErrorRecoveryAttempt({
      surfaceKey: context.surfaceKey,
      fingerprint: context.fingerprint,
    })
    context.attempt = attempt
    this.setState({
      phase: recoveryPhaseForAttempt(attempt, {
        canAskAgent: this.props.canAskAgent !== false,
      }),
      attemptPhase: attempt?.phase || null,
      repairChatId: attempt?.chatId || null,
      agentError: attempt?.phase === 'agent-failed'
        ? 'Repair chat request failed.'
        : '',
    })
  }

  handleRefresh = () => {
    if (this.agentStarting) return
    const context = this.crashContext
    if (context) {
      writeRefreshedRecoveryAttempt({
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
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
    if (!context || this.agentStarting || this.props.canAskAgent === false) return
    const identity = createRepairIdentity(context.attempt)
    const startingAttempt = {
      ...(context.attempt || {}),
      phase: 'agent-starting',
      repairRequestId: identity.repairRequestId,
      messageCid: identity.messageCid,
    }
    context.attempt = startingAttempt
    this.agentStarting = true
    this.repairController = new AbortController()
    writeErrorRecoveryAttempt({
      surfaceKey: context.surfaceKey,
      fingerprint: context.fingerprint,
      ...startingAttempt,
    })
    this.setState({
      phase: 'agent-starting',
      attemptPhase: 'agent-starting',
      agentError: '',
    })
    try {
      const result = await startAgentRepair({
        client: api,
        base: BASE,
        repairRequestId: identity.repairRequestId,
        messageCid: identity.messageCid,
        signal: this.repairController.signal,
        onChatCreated: chatId => {
          const attempt = { ...startingAttempt, chatId }
          context.attempt = attempt
          writeErrorRecoveryAttempt({
            surfaceKey: context.surfaceKey,
            fingerprint: context.fingerprint,
            ...attempt,
          })
          this.setState({ repairChatId: chatId })
        },
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
        repairRequestId: identity.repairRequestId,
        messageCid: identity.messageCid,
      })
      window.location.assign(result.path)
    } catch (error) {
      this.agentStarting = false
      this.repairController = null
      if (error?.name === 'AbortError') return
      const chatId = context.attempt?.chatId || null
      context.attempt = {
        phase: 'agent-failed',
        chatId,
        repairRequestId: identity.repairRequestId,
        messageCid: identity.messageCid,
      }
      writeErrorRecoveryAttempt({
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
        ...context.attempt,
      })
      this.setState({
        phase: 'recovery',
        attemptPhase: 'agent-failed',
        repairChatId: chatId,
        agentError: 'Repair chat request failed.',
      })
    }
  }

  render() {
    if (!this.state.error) return this.props.children
    const message = redactDiagnosticText(this.state.error?.message || this.state.error)
    const cls = this.props.variant === 'inline' ? 'errbound errbound--inline' : 'errbound'
    const { phase, attemptPhase, repairChatId } = this.state
    const canAskAgent = this.props.canAskAgent !== false
    const actions = recoveryActionPolicy({
      phase,
      attemptPhase,
      canAskAgent,
      repairChatId,
    })
    return (
      <div className={cls}>
        <div className="errbound__card">
          <h1
            className="errbound__title"
            ref={node => { this.headingRef = node }}
            tabIndex={-1}
          >
            Something broke
          </h1>
          <p className="errbound__body">
            {phase === 'refresh' && (
              <>This screen hit an unexpected error. Refreshing won’t delete your chats.</>
            )}
            {(phase === 'agent' || phase === 'agent-starting') && (
              <>Refreshing didn’t fix this screen. Möbius can start a new repair chat and share these technical details with your agent to investigate and fix it.</>
            )}
            {actions.showRecovery && attemptPhase === 'agent-directed' && repairChatId && (
              <>The repair chat started, but this screen still can’t open. System recovery is the remaining fallback.</>
            )}
            {actions.showRecovery && attemptPhase === 'agent-failed' && (
              <>The repair chat couldn’t start. You can retry it or use system recovery as a last resort.</>
            )}
            {actions.showRecovery && !canAskAgent && (
              <>Refreshing didn’t fix this screen. Use system recovery to diagnose the problem without relying on this embedded chat.</>
            )}
          </p>
          <details className="errbound__details">
            <summary>Technical details</summary>
            <pre className="errbound__detail">{message}</pre>
          </details>
          {(phase === 'agent-starting' || this.state.agentError) && (
            <p
              className={`errbound__status${this.state.agentError ? ' errbound__status--sr-only' : ''}`}
              role="status"
              aria-live="polite"
            >
              {phase === 'agent-starting' ? 'Starting the repair chat. This may take a moment.' : this.state.agentError}
            </p>
          )}
          <div
            className="errbound__actions"
            aria-busy={phase === 'agent-starting' ? true : undefined}
          >
            {actions.showRefreshAgain && (
              <button type="button" className="errbound__btn" onClick={this.handleRefresh}>
                Refresh again
              </button>
            )}
            {actions.showOpenRepairChat && (
              <a className="errbound__btn" href={repairChatPath(repairChatId, BASE)}>
                Open repair chat
              </a>
            )}
            {(phase === 'refresh' || actions.showAskAgent || actions.showRetryAgent || phase === 'agent-starting') && (
              <button
                type="button"
                className="errbound__btn errbound__btn--primary"
                onClick={phase === 'refresh' ? this.handleRefresh : this.handleAgentRepair}
                disabled={phase === 'agent-starting'}
              >
                {phase === 'refresh' && 'Refresh screen'}
                {phase === 'agent' && (attemptPhase === 'agent-starting' ? 'Resume repair chat' : 'Start repair chat')}
                {phase === 'agent-starting' && 'Starting repair chat…'}
                {actions.showRetryAgent && 'Retry repair chat'}
              </button>
            )}
          </div>
          {actions.showRecovery && (
            <RecoveryLink lead="If the repair chat can’t get you back in," />
          )}
        </div>
      </div>
    )
  }
}
