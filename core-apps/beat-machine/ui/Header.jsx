function PadMark(props) {
  return (
    <span {...props}>
      <span /><span /><span /><span />
    </span>
  )
}

export function BeatHeader({ appId, readyCount, online }) {
  return (
    <header className="bm-header">
      <div className="bm-brand">
        {appId && (
          <img
            src={`/api/apps/${appId}/icon?size=64`}
            alt=""
            width={36}
            height={36}
            className="bm-brand-icon"
            onError={(event) => {
              event.currentTarget.style.display = 'none'
              const fallback = event.currentTarget.nextElementSibling
              if (fallback) fallback.style.display = 'grid'
            }}
          />
        )}
        <PadMark className="bm-mark" style={{ display: appId ? 'none' : 'grid' }} aria-hidden="true" />
        <div className="bm-brand-text">
          <h1 className="bm-title">Beat Machine</h1>
          <span className="bm-subtitle">{readyCount} pads ready</span>
        </div>
      </div>
      <div className="bm-header-right">
        {!online && <span className="bm-sync-pill" role="status">Offline</span>}
      </div>
    </header>
  )
}
