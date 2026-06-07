// [kaveri fork] Native desktop (Mac/Electron) notification for a completed
// reply when the window isn't focused — the "your answer's ready" nudge when
// you've switched away. iOS is intentionally excluded: it gets APNs push from
// the gateway instead (the WebView can't notify while backgrounded anyway).
// Best-effort and never throws.

let permissionAsked = false

export function notifyDesktopReply(text: string, title = 'Kaveri'): void {
  try {
    if (typeof document === 'undefined' || typeof Notification === 'undefined') {
      return
    }
    // iOS uses server-sent APNs; don't double up (and Notification is a no-op
    // in WKWebView anyway).
    if (document.documentElement.dataset.platform === 'ios') {
      return
    }
    // Only nudge when the user has looked away — otherwise they're watching it.
    if (document.hasFocus()) {
      return
    }

    const body = (text || '').replace(/\s+/g, ' ').trim()
    if (!body) {
      return
    }
    const snippet = body.length > 160 ? `${body.slice(0, 160)}…` : body

    // Prefer Electron's main-process notification (reliably registers the app
    // with macOS NotificationCenter; the renderer web Notification API didn't).
    const bridge = (window as unknown as { hermesDesktop?: { notify?: (o: { title: string; body: string }) => unknown } })
      .hermesDesktop
    if (bridge?.notify) {
      void bridge.notify({ title, body: snippet })
      return
    }

    const show = () => {
      const n = new Notification(title, { body: snippet })
      n.onclick = () => {
        try {
          window.focus()
        } catch {
          /* best-effort */
        }
      }
    }

    if (Notification.permission === 'granted') {
      show()
    } else if (Notification.permission === 'default' && !permissionAsked) {
      permissionAsked = true
      void Notification.requestPermission().then(p => {
        if (p === 'granted') {
          show()
        }
      })
    }
  } catch {
    /* notifications are best-effort */
  }
}
