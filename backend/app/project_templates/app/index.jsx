import { useEffect, useState } from 'react'

const TOTAL = 25 * 60
const CSS = `
*{box-sizing:border-box}html,body,#root{min-height:100%}body{margin:0}.app{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 20% 0,#dceee3,transparent 36%),#f6f5f0;color:#18201c;font:15px/1.5 ui-sans-serif,system-ui,sans-serif}.card{width:min(560px,100%);padding:32px;border:1px solid #dfe2dd;border-radius:28px;background:#fffffff0;box-shadow:0 24px 70px #24433218}header{display:flex;align-items:center;justify-content:space-between;gap:20px}.eyebrow{margin:0;color:#276f51;font-size:.72rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase}h1{margin:4px 0 0;font-size:clamp(1.7rem,6vw,2.4rem);letter-spacing:-.04em}.pill{padding:8px 11px;border-radius:999px;background:#e7f1eb;color:#215d45;font-size:.78rem;font-weight:700}.timer{text-align:center;padding:44px 0 10px}.time{font-variant-numeric:tabular-nums;font-size:clamp(4.7rem,19vw,7rem);font-weight:760;letter-spacing:-.08em;line-height:1}.caption{margin:10px 0;color:#69716d}.controls{display:flex;justify-content:center;gap:10px;flex-wrap:wrap}button{min-height:44px;padding:0 18px;border:1px solid #dfe2dd;border-radius:14px;background:#fff;color:inherit;font:inherit;font-weight:700;cursor:pointer}button.primary{min-width:124px;border-color:#276f51;background:#276f51;color:white}button:focus-visible{outline:3px solid #7dbf9f;outline-offset:2px}.progress{height:7px;margin-top:30px;overflow:hidden;border-radius:99px;background:#e3e5e0}.progress span{display:block;height:100%;background:#276f51;transition:width .4s ease}@media(prefers-reduced-motion:no-preference){button{transition:transform .14s ease}button:hover{transform:translateY(-1px)}}
`

export default function App() {
  const [remaining, setRemaining] = useState(TOTAL)
  const [running, setRunning] = useState(false)
  useEffect(() => {
    if (!running) return undefined
    const timer = window.setInterval(() => setRemaining(value => {
      if (value <= 1) { setRunning(false); return 0 }
      return value - 1
    }), 1000)
    return () => window.clearInterval(timer)
  }, [running])
  const time = `${String(Math.floor(remaining / 60)).padStart(2, '0')}:${String(remaining % 60).padStart(2, '0')}`
  return <main className="app"><style>{CSS}</style><section className="card"><header><div><p className="eyebrow">Mini-app starter</p><h1>Focus timer</h1></div><span className="pill">25 minute sprint</span></header><div className="timer" aria-live="polite"><div className="time">{time}</div><p className="caption">{remaining === 0 ? 'Sprint complete. Take a breath.' : running ? 'You are in the work. Keep going.' : 'Make space for one important thing.'}</p><div className="controls"><button type="button" onClick={() => { setRunning(false); setRemaining(TOTAL) }}>Reset</button><button type="button" className="primary" onClick={() => { if (remaining === 0) setRemaining(TOTAL); setRunning(value => !value) }}>{running ? 'Pause' : remaining === TOTAL ? 'Start' : remaining === 0 ? 'Again' : 'Resume'}</button></div><div className="progress"><span style={{ width: `${100 * (TOTAL - remaining) / TOTAL}%` }} /></div></div></section></main>
}
