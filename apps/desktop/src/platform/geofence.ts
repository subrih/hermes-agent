// [kaveri fork] Register background geofences on iOS. Fetches the regions the
// gateway is configured to watch (GET /api/geofences) and hands them — plus the
// raw connection details — to the native HermesGeofence plugin, which monitors
// them and POSTs /api/event on enter/exit (even when the app is killed). No-op
// off Capacitor. See ios/App/App/HermesGeofencePlugin.swift + the /api/event
// handler in hermes_cli/web_server.py.

import { Capacitor, registerPlugin } from '@capacitor/core'

import { getNativeConnection } from './ios-bridge'

interface HermesGeofencePlugin {
  configure(opts: {
    url: string
    token: string
    cfId: string
    cfSecret: string
    regions: Array<{ id: string; lat: number; lon: number; radius?: number }>
  }): Promise<{ ok: boolean; count: number }>
}

const HermesGeofence = registerPlugin<HermesGeofencePlugin>('HermesGeofence')

function isNative(): boolean {
  try {
    return Capacitor.isNativePlatform()
  } catch {
    return false
  }
}

export async function initGeofences(): Promise<void> {
  if (!isNative()) {
    return
  }
  try {
    const conn = await getNativeConnection()
    if (!conn) {
      return
    }
    const api = (window as unknown as { hermesDesktop?: { api?: (o: unknown) => Promise<unknown> } }).hermesDesktop?.api
    if (!api) {
      return
    }
    const res = (await api({ path: '/api/geofences', method: 'GET' })) as
      | { regions?: Array<{ id: string; lat: number; lon: number; radius?: number }> }
      | undefined
    const regions = res?.regions ?? []
    await HermesGeofence.configure({
      url: conn.url,
      token: conn.token,
      cfId: conn.cfId,
      cfSecret: conn.cfSecret,
      regions
    })
  } catch (err) {
    console.warn('[geofence] init failed', err)
  }
}
