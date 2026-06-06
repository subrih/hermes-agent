// [kaveri fork] iOS mobile-UX behaviors that CSS can't express. The responsive
// layout itself lives in mobile-ios.css (scoped [data-platform="ios"]); this
// handles the interaction state: open the app on the chat (side panes closed)
// and dismiss the sidebar drawer once a session is picked. New file, no
// component edits — additive, and a no-op off Capacitor.

import { Capacitor } from '@capacitor/core'

import { FILE_BROWSER_PANE_ID, setSidebarOpen } from '@/store/layout'
import { setPaneOpen } from '@/store/panes'
import { $selectedStoredSessionId } from '@/store/session'

function isNative(): boolean {
  try {
    return Capacitor.isNativePlatform()
  } catch {
    return false
  }
}

export function initMobileUi(): void {
  if (!isNative()) return

  // Start on the chat with every side pane closed (the mobile expectation —
  // desktop persists pane state, but a phone should open to the conversation).
  setSidebarOpen(false)
  setPaneOpen(FILE_BROWSER_PANE_ID, false)
  setPaneOpen('preview', false)

  // Picking a session closes the drawer so the conversation is visible.
  let prev = $selectedStoredSessionId.get()
  $selectedStoredSessionId.subscribe(id => {
    if (id && id !== prev) setSidebarOpen(false)
    prev = id
  })

  // Keep the app height pinned to the visual viewport so the composer rides
  // above the on-screen keyboard (this WKWebView doesn't resize for it). The
  // shell consumes --app-height in mobile-ios.css.
  const vv = window.visualViewport
  if (vv) {
    const sync = () => document.documentElement.style.setProperty('--app-height', `${vv.height}px`)
    sync()
    vv.addEventListener('resize', sync)
    vv.addEventListener('scroll', sync)
  }
}
