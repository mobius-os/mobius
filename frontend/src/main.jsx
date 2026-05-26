import React from 'react'
import ReactDOM from 'react-dom/client'
// Apps-SDK-UI tokens (--switch-*, --color-ring, etc.) — required
// for SDK components like Switch to render with their canonical
// styles. Imported BEFORE index.css so Möbius-specific overrides
// can win without `!important`.
import '@openai/apps-sdk-ui/css'
import App from './App.jsx'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
