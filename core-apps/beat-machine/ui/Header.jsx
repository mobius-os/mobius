import { S } from '../styles.js'

export function Header({ appId, activePads, online }) {
  const soundCount = `${activePads} ${activePads === 1 ? 'sound' : 'sounds'}`

  return (
    <header style={S.header}>
      <div style={S.titleRow}>
        {appId ? (
          <>
            <img
              src={`/api/apps/${appId}/icon?size=64`}
              alt=""
              width={30}
              height={30}
              style={S.appIcon}
              onError={(event) => {
                event.currentTarget.style.display = 'none'
                const fallback = event.currentTarget.nextElementSibling
                if (fallback) fallback.style.display = 'flex'
              }}
            />
            <span style={{ ...S.logoFallback, display: 'none' }} aria-hidden="true">♬</span>
          </>
        ) : (
          <span style={S.logoFallback} aria-hidden="true">♬</span>
        )}
        <h1 style={S.title}>Beat Machine</h1>
        <span style={S.badge} title={`${soundCount} loaded`} aria-label={`${soundCount} loaded`}>
          {soundCount}
        </span>
      </div>
      {!online && <span style={S.offlinePill} role="status">Offline</span>}
    </header>
  )
}
