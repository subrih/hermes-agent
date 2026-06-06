import './styles.css'
import './mobile-ios.css' // [kaveri fork] iOS responsive overrides (scoped [data-platform="ios"])

import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'

import App from './app'
import { ErrorBoundary } from './components/error-boundary'
import { HapticsProvider } from './components/haptics-provider'
import { I18nProvider } from './i18n'
import { installClipboardShim } from './lib/clipboard'
import { queryClient } from './lib/query-client'
import { initIosBridge } from './platform/ios-bridge' // [kaveri fork] no-op off Capacitor
import { runLiveUpdate } from './platform/live-update' // [kaveri fork] no-op off Capacitor
import { initMobileUi } from './platform/mobile-ui' // [kaveri fork] no-op off Capacitor
import { ThemeProvider } from './themes/context'

installClipboardShim()

// Dev-only: install __PERF_DRIVE__ + __PERF_PROBE__ on window so the
// scripts/ harnesses can drive a synthetic stream + record render cost.
// Tree-shaken out of production builds. (Uses MODE rather than DEV because
// our Vite setup currently bundles with PROD=true even in `vite dev`; see
// scripts/dev-no-hmr.mjs for the surrounding workarounds.)
if (import.meta.env.MODE !== 'production') {
  import('./app/chat/perf-probe')
}

// [kaveri fork] On iOS (Capacitor) install the native `window.hermesDesktop`
// bridge BEFORE first render so the boot/connection flow finds it. No-op on
// desktop (Electron's preload already provides the bridge).
async function bootstrap() {
  await initIosBridge()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <ErrorBoundary label="root">
        <QueryClientProvider client={queryClient}>
          <I18nProvider>
            <ThemeProvider>
              <HapticsProvider>
                <HashRouter>
                  <App />
                </HashRouter>
              </HapticsProvider>
            </ThemeProvider>
          </I18nProvider>
        </QueryClientProvider>
      </ErrorBoundary>
    </StrictMode>
  )
  // [kaveri fork] iOS: confirm bundle health + pull any newer web bundle (OTA).
  void runLiveUpdate()
  // [kaveri fork] iOS: mobile interaction defaults (open on chat, drawer dismiss).
  initMobileUi()
}

void bootstrap()
