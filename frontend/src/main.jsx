import React from 'react'
import ReactDOM from 'react-dom/client'
// `@openai/apps-sdk-ui/css` is `@import`-ed from inside index.css
// so Tailwind v4 processes the SDK's `@theme static {}` token
// blocks alongside our own CSS — importing it here as a JS module
// kept it outside Tailwind's pipeline and the SDK tokens silently
// resolved to empty (`--radius-full` returned "", so SDK Switch
// thumbs rendered as squares instead of circles).
import App from './App.jsx'
import { installGlobalErrorHandlers } from './lib/errorLog.js'
import { SHELL_BUILD } from './lib/buildInfo.js'
import './index.css'

// Capture errors React's ErrorBoundary can't see (async/event-handler throws,
// unhandled promise rejections) so no failure white-screens or vanishes
// without a trace.
installGlobalErrorHandlers()

// Surface the shell build marker once at startup. This also keeps
// SHELL_BUILD referenced so the bundler can't tree-shake it away — its
// whole job is to move the entry bundle's content hash (see buildInfo.js).
console.info(`Mobius shell build: ${SHELL_BUILD}`)

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
