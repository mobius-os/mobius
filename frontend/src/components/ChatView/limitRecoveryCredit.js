// A paused provider-limit turn can be retried before its advertised reset only
// when the provider itself reports a separate paid allowance. Keep this purely
// advisory: a provider remains the authority at execution time, and the click
// is deliberate because it may incur a charge.
export function limitRecoveryCredit(provider, snapshot) {
  if (!provider || snapshot?.state !== 'ready') return null

  if (provider === 'claude') {
    const extra = snapshot.extra_usage
    if (extra?.available === true) {
      return {
        label: 'Paid extra usage is available',
        actionLabel: 'Continue with extra usage',
      }
    }
    return null
  }

  if (provider === 'codex' && typeof snapshot.credit_balance === 'string'
    && snapshot.credit_balance.trim()) {
    return {
      label: 'Paid credits are available',
      actionLabel: 'Continue with paid credits',
    }
  }

  if (provider === 'mobius') {
    const credits = Array.isArray(snapshot.windows)
      ? snapshot.windows.find(window => window?.kind === 'api_credits')
      : null
    if (Number(credits?.remaining_percent) > 0) {
      return {
        label: 'Usage credits are available',
        actionLabel: 'Continue with available credits',
      }
    }
  }

  return null
}
