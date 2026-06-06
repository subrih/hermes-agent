// [kaveri fork] Over-the-air web-bundle updates for the iOS (Capacitor) build,
// self-hosted + manual (Capgo updater, autoUpdate:false in capacitor.config).
// On launch we mark the running bundle healthy (notifyAppReady → enables Capgo's
// auto-rollback if a future bundle is broken), then check our OTA channel for a
// newer web bundle and, if found, download + activate it. The channel URL is
// unguessable because the bundle currently embeds the connection config.
// Desktop never runs this. No-op off Capacitor.

import { Capacitor, CapacitorHttp } from '@capacitor/core'
import { CapacitorUpdater } from '@capgo/capacitor-updater'

const CHANNEL = 'https://ota.hellopulse.ai/a3301684eb130bc7a0e7da61'

function isNative(): boolean {
  try {
    return Capacitor.isNativePlatform()
  } catch {
    return false
  }
}

const log = (msg: string) => console.info('[live-update]', msg)

export async function runLiveUpdate(): Promise<void> {
  if (!isNative()) return

  // Fire-and-forget: with autoUpdate:false, notifyAppReady can hang on its
  // internal semaphore ("Semaphore wait timed out") — never let it block the
  // update check. It only needs to mark the running bundle healthy for rollback.
  void CapacitorUpdater.notifyAppReady().catch(e => log('notifyAppReady error: ' + String(e)))

  try {
    const res = await CapacitorHttp.request({
      url: `${CHANNEL}/version.json`,
      method: 'GET',
      headers: { 'Cache-Control': 'no-cache' },
      connectTimeout: 6000,
      readTimeout: 6000
    })
    if (res.status !== 200) return
    const latest = typeof res.data === 'string' ? JSON.parse(res.data) : res.data
    if (!latest?.version || !latest?.url) return

    const current = await CapacitorUpdater.current()
    if (current?.bundle?.version === latest.version) return

    log(`updating ${current?.bundle?.version ?? 'builtin'} → ${latest.version}`)
    const bundle = await CapacitorUpdater.download({ url: latest.url, version: latest.version })
    await CapacitorUpdater.set(bundle) // reloads the webview into the new bundle
  } catch (e) {
    log('error: ' + String(e))
  }
}
