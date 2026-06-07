import type { CapacitorConfig } from '@capacitor/cli'

// Kaveri mobile = the Hermes desktop React renderer wrapped in Capacitor.
// webDir points at the desktop app's Vite build so we ship the exact same
// bundle (vite base is already './', WKWebView-compatible). The iOS bridge
// (window.hermesDesktop shim) is part of that bundle and self-activates under
// Capacitor; native HTTP/WS + CF Access headers are provided by Capacitor.
const config: CapacitorConfig = {
  appId: 'com.kaveri.cockpit',
  appName: 'Kaveri',
  webDir: '../desktop/dist',
  ios: {
    // [kaveri fork] 'never' = WKWebView renders edge-to-edge and does NOT apply
    // its own scroll-view safe-area inset. We handle safe areas precisely in CSS
    // via env(safe-area-inset-*) (viewport-fit=cover). 'always' fought env()
    // (reported 0 until a layout pass), forcing hardcoded gutter fallbacks.
    contentInset: 'never'
  },
  plugins: {
    // [kaveri fork] Live web-bundle updates (Capgo), manual/self-hosted: we
    // drive download+set ourselves from our OTA server. autoUpdate off so the
    // plugin doesn't phone Capgo's cloud. See src/platform/live-update.ts.
    CapacitorUpdater: {
      autoUpdate: false
    },
    // [kaveri fork] Native keyboard resize: the WKWebView frame itself shrinks
    // when the keyboard appears, so the layout (100vh) tracks it and the
    // composer stays above the keyboard — no web-side viewport hacks, no
    // scroll-to-focus glitches.
    Keyboard: {
      resize: 'native'
    }
  }
}

export default config
