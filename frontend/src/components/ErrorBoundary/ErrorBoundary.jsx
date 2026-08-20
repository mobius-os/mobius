import { Component } from 'react'
import { api, BASE } from '../../api/client.js'
import { redactDiagnosticText } from '../../lib/diagnosticRedaction.js'
import { recordClientError } from '../../lib/errorLog.js'
import {
  buildAgentRepairPrompt,
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
  runAgentRepair,
  writeRefreshedRecoveryAttempt,
} from '../../lib/errorRecovery.js'
import { reloadIfGenerationStale } from '../../lib/shellUpdate.js'
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
 *   onError   — optional; called when recovery replaces the guarded subtree
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
    attempt: null,
    repairActive: false,
    selfHealing: false,
  }

  crashContext = null
  repairController = null
  pageShowListening = false
  headingRef = null

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    clearTimeout(this.stableTimer)
    this.props.onError?.(error)
    // Record through the shared client-error log (console + owner-readable ring
    // buffer), same sink the global window handlers use.
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
    if (attempt) {
      // A recovery attempt for THIS exact crash is already on record — the
      // stale-generation self-heal (or a manual refresh) has already run once
      // and it still failed. Do not auto-reload again; show the recovery panel
      // so the escalation (refresh → ask agent) proceeds. This ledger is the
      // loop guard that keeps a genuine bug from reload-looping.
      this.setState({ attempt, repairActive: false }, () => this.headingRef?.focus())
    } else {
      // First occurrence: if a newer shell generation exists this is a
      // stale-bundle crash — silently reload onto the fixed generation.
      // Otherwise fall through to the panel (genuine failure on the newest build).
      this.setState({ selfHealing: true }, () => this.headingRef?.focus())
      this.selfHealIfStale()
    }
  }

  // Recovery reload shared by auto-heal and the manual refresh: escape a stale
  // generation through the SW handoff, reloading via applyRecoveryReload.
  // Resolves true when a newer generation was found and a reload was initiated.
  recoverReload = (context) => reloadIfGenerationStale({
    serviceWorker: typeof navigator !== 'undefined' ? navigator.serviceWorker : null,
    reload: () => this.applyRecoveryReload(context),
  })

  selfHealIfStale = async () => {
    const context = this.crashContext
    let healing = false
    try {
      healing = await this.recoverReload(context)
    } catch {
      healing = false
    }
    // No newer generation: the running build itself is broken. Drop the
    // "updating" state and show the recovery panel (manual refresh + ask agent).
    // The identity guard skips this if a newer crash has since replaced context.
    if (!healing && this.crashContext === context) {
      this.setState(
        { selfHealing: false, attempt: context.attempt, repairActive: false },
        () => this.headingRef?.focus(),
      )
    }
  }

  // Single reload executor for both auto-heal and the manual refresh button:
  // record the attempt (loop guard for a repeat crash), notify the owning
  // surface, mark the reload so App.jsx skips the splash, then reload.
  applyRecoveryReload = (context) => {
    if (context) {
      writeRefreshedRecoveryAttempt({
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
      })
    }
    this.props.onReset?.()
    try {
      sessionStorage.setItem('shell-reload', '1')
    } catch {
      /* ignore */
    }
    window.location.reload()
  }

  componentWillUnmount() {
    const controller = this.repairController
    this.repairController = null
    controller?.abort()
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
    const controller = this.repairController
    this.repairController = null
    controller?.abort()
    const attempt = readErrorRecoveryAttempt({
      surfaceKey: context.surfaceKey,
      fingerprint: context.fingerprint,
    })
    context.attempt = attempt
    this.setState({ attempt, repairActive: false })
  }

  handleRefresh = async () => {
    if (this.repairController) return
    const context = this.crashContext
    // Escape a stale generation if one exists; otherwise honor the refresh with a
    // plain reload. A blind reload alone can be answered by the outgoing worker's
    // precache and land back on the same stale bundle.
    if (!(await this.recoverReload(context))) this.applyRecoveryReload(context)
  }

  handleAgentRepair = async () => {
    const context = this.crashContext
    if (!context || this.repairController || this.props.canAskAgent === false) return
    const controller = new AbortController()
    this.repairController = controller
    this.setState({ repairActive: true })
    try {
      const result = await runAgentRepair({
        client: api,
        base: BASE,
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
        previousAttempt: context.attempt,
        signal: controller.signal,
        onAttempt: (attempt) => {
          context.attempt = attempt
          this.setState({ attempt })
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
      if (this.repairController === controller) {
        this.repairController = null
        this.setState({ repairActive: false })
      }
    }
  }

  render() {
    if (!this.state.error) return this.props.children
    const cls = this.props.variant === 'inline' ? 'errbound errbound--inline' : 'errbound'
    if (this.state.selfHealing) {
      // Stale-generation self-heal in flight: the page is reloading onto the
      // fixed build, so show a quiet status instead of flashing "Something broke".
      return (
        <div className={cls}>
          <div className="errbound__card errbound__updating" role="status" aria-live="polite">
            <span
              className="errbound__updating-text"
              tabIndex={-1}
              ref={node => { this.headingRef = node }}
            >
              Updating to the latest version…
            </span>
          </div>
        </div>
      )
    }
    const message = redactDiagnosticText(this.state.error?.message || this.state.error)
    return (
      <div className={cls}>
        <RecoveryPanel
          variant="boundary"
          className="errbound__card"
          headingRef={node => { this.headingRef = node }}
          title="Something broke"
          subject="screen"
          diagnostic={message}
          attempt={this.state.attempt}
          repairActive={this.state.repairActive}
          canAskAgent={this.props.canAskAgent !== false}
          refreshLabel="Refresh screen"
          onRefresh={this.handleRefresh}
          onAgentRepair={this.handleAgentRepair}
        />
      </div>
    )
  }
}
