// [kaveri fork] iOS device location. Keeps a recent fix in memory (refreshed via
// a position watch + on foreground) so each turn can attach {lat,lon,accuracy,ts}
// to prompt.submit without paying a per-turn GPS hit. The gateway turns it into
// an ephemeral "near me / directions" context note. No-op off Capacitor.

import { Capacitor } from '@capacitor/core'

export interface LocationFix {
  lat: number
  lon: number
  accuracy?: number
  ts: number
}

const MAX_AGE_MS = 15 * 60 * 1000 // don't send a fix older than this

let latest: LocationFix | null = null

function isNative(): boolean {
  try {
    return Capacitor.isNativePlatform()
  } catch {
    return false
  }
}

/** Most recent fix, or null if none / too stale to be useful. */
export function getLatestLocation(): LocationFix | null {
  if (latest && Date.now() - latest.ts <= MAX_AGE_MS) {
    return latest
  }
  return null
}

export async function initLocation(): Promise<void> {
  if (!isNative()) {
    return
  }
  try {
    const { Geolocation } = await import('@capacitor/geolocation')

    let perm = await Geolocation.checkPermissions()
    if (perm.location === 'prompt' || perm.location === 'prompt-with-rationale') {
      perm = await Geolocation.requestPermissions()
    }
    if (perm.location !== 'granted' && perm.coarseLocation !== 'granted') {
      return
    }

    const apply = (pos: { coords: { latitude: number; longitude: number; accuracy?: number } }) => {
      latest = {
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        accuracy: pos.coords.accuracy,
        ts: Date.now()
      }
    }

    const refresh = (maxAge: number) =>
      Geolocation.getCurrentPosition({ enableHighAccuracy: false, timeout: 10000, maximumAge: maxAge })
        .then(apply)
        .catch(() => undefined)

    await refresh(300000) // seed with a recent-ish fix
    // Keep it current while the app is open.
    await Geolocation.watchPosition({ enableHighAccuracy: false }, pos => {
      if (pos) {
        apply(pos)
      }
    })
    // And re-grab on foreground (watch may pause while backgrounded).
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        void refresh(60000)
      }
    })
  } catch (err) {
    console.warn('[location] init failed', err)
  }
}
