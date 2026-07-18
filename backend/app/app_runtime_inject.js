import { createElement, Fragment, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { init as initMobiusRuntime } from 'mobius-runtime'

// Esbuild injects this module into every compiled mini-app. The imports above
// are bundled with the app, so the opaque frame can mount it from one brokered
// blob without fetching React—or any other runtime package—over the network.
const config = globalThis.__mobiusRuntimeConfig
if (config && typeof config === 'object') initMobiusRuntime(config)

globalThis.__mobiusCompiledRuntime = Object.freeze({
  abi: 1,
  createElement,
  createRoot,
  Fragment,
  initMobiusRuntime,
  useEffect,
})
