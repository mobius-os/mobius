import { Component } from 'react'
import { api, BASE } from '../../api/client.js'
import { recordClientError } from '../../lib/errorLog.js'
import {
  buildAgentRepairPrompt,
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
  redactDiagnosticText,
  recoveryViewForAttempt,
  runAgentRepair,
  writeRefreshedRecoveryAttempt,
} from '../../lib/errorRecovery.js'
import RecoveryPanel from './RecoveryPanel.jsx'
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
    repairChatId: null,
    attemptPhase: null,
  }

  crashContext = null
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
    this.setState(recoveryViewForAttempt(attempt, {
      canAskAgent: this.props.canAskAgent !== false,
    }))
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
    this.repairController = null
    const attempt = readErrorRecoveryAttempt({
      surfaceKey: context.surfaceKey,
      fingerprint: context.fingerprint,
    })
    context.attempt = attempt
    this.setState(recoveryViewForAttempt(attempt, {
      canAskAgent: this.props.canAskAgent !== false,
    }))
  }

  handleRefresh = () => {
    if (this.repairController) return
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
    if (!context || this.repairController || this.props.canAskAgent === false) return
    const controller = new AbortController()
    this.repairController = controller
    try {
      const result = await runAgentRepair({
        client: api,
        base: BASE,
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
        previousAttempt: context.attempt,
        signal: controller.signal,
        onAttempt: (attempt, { active }) => {
          context.attempt = attempt
          this.setState(recoveryViewForAttempt(attempt, {
            active,
            canAskAgent: this.props.canAskAgent !== false,
          }))
        },
        prompt: buildAgentRepairPrompt({
          surface: context.surfaceKey,
          message: context.message,
          componentStack: context.componentStack,
          pathname: window.location.pathname,
        }),
      })
      window.location.assign(result.path)
    } catch (error) {
      if (error?.name === 'AbortError') return
    } finally {
      if (this.repairController === controller) this.repairController = null
    }
  }

  render() {
    if (!this.state.error) return this.props.children
    const message = redactDiagnosticText(this.state.error?.message || this.state.error)
    const cls = this.props.variant === 'inline' ? 'errbound errbound--inline' : 'errbound'
    const { phase, attemptPhase, repairChatId } = this.state
    const canAskAgent = this.props.canAskAgent !== false
    return (
      <div className={cls}>
        <RecoveryPanel
          variant="boundary"
          className="errbound__card"
          headingRef={node => { this.headingRef = node }}
          title="Something broke"
          subject="screen"
          diagnostic={message}
          phase={phase}
          attemptPhase={attemptPhase}
          repairChatId={repairChatId}
          canAskAgent={canAskAgent}
          refreshLabel="Refresh screen"
          onRefresh={this.handleRefresh}
          onAgentRepair={this.handleAgentRepair}
        />
      </div>
    )
  }
}
