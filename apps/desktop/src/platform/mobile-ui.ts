// [kaveri fork] iOS mobile-UX behaviors that CSS can't express. The responsive
// layout itself lives in mobile-ios.css (scoped [data-platform="ios"]); this
// handles the interaction state: open the app on the chat (side panes closed)
// and dismiss the sidebar drawer once a session is picked. New file, no
// component edits — additive, and a no-op off Capacitor.

import { Capacitor } from '@capacitor/core'

import { $sidebarOpen, FILE_BROWSER_PANE_ID, setSidebarOpen } from '@/store/layout'
import { setPaneOpen } from '@/store/panes'
import { $selectedStoredSessionId } from '@/store/session'

function isNative(): boolean {
  try {
    return Capacitor.isNativePlatform()
  } catch {
    return false
  }
}

// Tap-to-dismiss backdrop behind the sidebar drawer. Created lazily, toggled by
// the $sidebarOpen store; styled in mobile-ios.css (#mobile-drawer-backdrop).
function installDrawerBackdrop(): void {
  const el = document.createElement('div')
  el.id = 'mobile-drawer-backdrop'
  el.addEventListener('click', () => setSidebarOpen(false))
  document.body.appendChild(el)
  $sidebarOpen.subscribe(open => {
    el.classList.toggle('is-open', open)
  })
}

// Suppress the composer's launch autofocus, which pops the keyboard the instant
// the app opens on a phone. Until the user's first real tap, blur any input that
// programmatically grabs focus; a user-initiated focus afterwards works normally.
function suppressLaunchAutofocus(): void {
  let userActed = false
  const onFocusIn = (e: FocusEvent) => {
    if (userActed) return
    const t = e.target as HTMLElement | null
    if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.isContentEditable)) {
      t.blur()
    }
  }
  const stop = () => {
    userActed = true
    document.removeEventListener('focusin', onFocusIn, true)
  }
  document.addEventListener('focusin', onFocusIn, true)
  document.addEventListener('pointerdown', stop, { capture: true, once: true })
  // Safety: lift the suppression after the launch window regardless.
  window.setTimeout(stop, 4000)
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

  installDrawerBackdrop()
  suppressLaunchAutofocus()

  // Anchor the app shell to the visual viewport box so the composer always rides
  // above the on-screen keyboard and snaps back when it hides. The shell is
  // position:fixed in mobile-ios.css; we feed it the viewport's height AND top
  // offset. Tracking offsetTop (not just height) is what fixes the "composer
  // climbs upstairs after dismissing the keyboard" bug — on a device the visual
  // viewport can be shifted, not only shrunk, and height alone misses that.
  const vv = window.visualViewport
  if (vv) {
    const root = document.documentElement.style
    const sync = () => {
      root.setProperty('--app-height', `${vv.height}px`)
      root.setProperty('--vv-top', `${vv.offsetTop}px`)
      window.scrollTo(0, 0)
    }
    sync()
    vv.addEventListener('resize', sync)
    vv.addEventListener('scroll', sync)
  }
}
