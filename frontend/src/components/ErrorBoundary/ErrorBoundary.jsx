import { Component } from 'react'
import useAgentRepair from '../../hooks/useAgentRepair.js'
import { redactDiagnosticText } from '../../lib/diagnosticRedaction.js'
import { recordClientError } from '../../lib/errorLog.js'
import {
  buildAgentRepairPrompt,
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
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
// The crash panel is a function component so the repair flow (ledger entry,
// in-flight request, bfcache restore) lives in the shared hook; the class
// above it only catches, self-heals a stale generation, and reloads.
function CrashRecovery({ context, canAskAgent, diagnostic, headingRef, onRefresh }) {
  const { attempt, repairActive, repair } = useAgentRepair({
    surfaceKey: context.surfaceKey,
    fingerprint: context.fingerprint,
    prompt: buildAgentRepairPrompt({
      surface: context.surfaceKey,
      message: context.message,
      componentStack: context.componentStack,
      pathname: window.location.pathname,
    }),
  })
  return (
    <RecoveryPanel
      variant="boundary"
      className="errbound__card"
      headingRef={headingRef}
      title="Something broke"
      subject="screen"
      diagnostic={diagnostic}
      attempt={attempt}
      repairActive={repairActive}
      canAskAgent={canAskAgent}
      refreshLabel="Refresh screen"
      onRefresh={() => { if (!repairActive) onRefresh() }}
      onAgentRepair={repair}
    />
  )
}

export default class ErrorBoundary extends Component {
  state = {
    error: null,
    selfHealing: false,
  }

  crashContext = null
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
    this.crashContext = { surfaceKey, fingerprint, message, componentStack }
    if (readErrorRecoveryAttempt({ surfaceKey, fingerprint })) {
      // A recovery attempt for THIS exact crash is already on record — the
      // stale-generation self-heal (or a manual refresh) has already run once
      // and it still failed. Do not auto-reload again; show the recovery panel
      // so the escalation (refresh → ask agent) proceeds. This ledger is the
      // loop guard that keeps a genuine bug from reload-looping.
      this.setState({ selfHealing: false }, () => this.headingRef?.focus())
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
      this.setState({ selfHealing: false }, () => this.headingRef?.focus())
    }
  }

  // Single reload executor for both auto-heal and the manual refresh button:
  // record the attempt (loop guard for a repeat crash), notify the owning
  // surface, then reload through the ordinary launch cover.
  applyRecoveryReload = (context) => {
    if (context) {
      writeRefreshedRecoveryAttempt({
        surfaceKey: context.surfaceKey,
        fingerprint: context.fingerprint,
      })
    }
    this.props.onReset?.()
    window.location.reload()
  }

  surfaceKey = () => this.props.recoveryKey || this.props.label || 'app'

  handleRefresh = async () => {
    const context = this.crashContext
    // Escape a stale generation if one exists; otherwise honor the refresh with a
    // plain reload. A blind reload alone can be answered by the outgoing worker's
    // precache and land back on the same stale bundle.
    if (!(await this.recoverReload(context))) this.applyRecoveryReload(context)
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
    // The render React schedules from getDerivedStateFromError precedes
    // componentDidCatch; that frame has no crash context yet and is never
    // painted, because componentDidCatch's setState re-renders before paint.
    if (!this.crashContext) return null
    return (
      <div className={cls}>
        <CrashRecovery
          context={this.crashContext}
          canAskAgent={this.props.canAskAgent !== false}
          diagnostic={redactDiagnosticText(this.state.error?.message || this.state.error)}
          headingRef={node => { this.headingRef = node }}
          onRefresh={this.handleRefresh}
        />
      </div>
    )
  }
}
