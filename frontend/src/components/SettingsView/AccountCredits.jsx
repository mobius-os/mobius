/**
 * The Credits page, opened from the Möbius account page's Credits row: the
 * wallet (or Connect credits for a Möbius launched from mobius.you that is not
 * connected yet), then the whole history last, in a scrolling window.
 *
 * Mounted only while the Credits page is open, so these reads run only then.
 * The caller renders it for the owner only: a shared-browser guest neither
 * sees nor reads credits.
 */
import { creditsSection } from '../../lib/creditsWallet.js'
import ActivityWindow, { RecentActivity } from './ActivityWindow.jsx'
import WalletSection, { WalletConnect } from './WalletSection.jsx'
import { useWallet } from './useWallet.js'

export default function AccountCredits({ active = true }) {
  const wallet = useWallet({ active })
  const section = creditsSection(wallet.summary)

  if (section === 'connect') {
    return <WalletConnect busy={wallet.busy} refusal={wallet.refusal} onConnect={wallet.pairWithAccount} />
  }
  if (section !== 'wallet') {
    if (!wallet.summary) return <p className="settings-message" role="status">Loading your credits…</p>
    return wallet.summary.mode === 'unknown'
      ? <p className="settings-message" role="alert">Your credits could not be read just now. Try again in a moment.</p>
      : <p className="settings-message">Credits are not available on this Möbius.</p>
  }
  return (
    <>
      <WalletSection
        summary={wallet.summary}
        terms={wallet.terms}
        busy={wallet.busy}
        notice={wallet.notice}
        refusal={wallet.refusal}
        onBuy={wallet.buy}
        onReconnect={wallet.pairWithAccount}
      />
      {/* Last on the page: it can run long, so nothing sits below it. */}
      {wallet.summary.balance && (
        <RecentActivity>
          <ActivityWindow pairing={wallet.summary.pairing ?? null} refreshKey={wallet.summary.balance.balance_micro ?? null} />
        </RecentActivity>
      )}
    </>
  )
}
