// [kaveri fork] iOS push-notification registration. On launch (native only),
// request permission, register with APNs, and POST the device token to the
// gateway (/api/notifications/register) so it can send alerts when the agent
// replies while the app is backgrounded/closed. Foreground banners are
// suppressed by Capacitor's default behavior (you're already in the app).
// No-op off Capacitor.

import { Capacitor } from '@capacitor/core'

function isNative(): boolean {
  try {
    return Capacitor.isNativePlatform()
  } catch {
    return false
  }
}

async function registerToken(token: string): Promise<void> {
  if (!token) {
    return
  }
  try {
    const api = (window as unknown as { hermesDesktop?: { api?: (o: unknown) => Promise<unknown> } }).hermesDesktop?.api
    if (!api) {
      return
    }
    await api({ path: '/api/notifications/register', method: 'POST', body: { token, platform: 'ios' } })
  } catch (err) {
    console.warn('[push] token register failed', err)
  }
}

export async function initPushNotifications(): Promise<void> {
  if (!isNative()) {
    return
  }
  try {
    const { PushNotifications } = await import('@capacitor/push-notifications')

    let receive = (await PushNotifications.checkPermissions()).receive
    if (receive === 'prompt' || receive === 'prompt-with-rationale') {
      receive = (await PushNotifications.requestPermissions()).receive
    }
    if (receive !== 'granted') {
      return
    }

    await PushNotifications.addListener('registration', token => {
      void registerToken(token.value)
    })
    await PushNotifications.addListener('registrationError', err => {
      console.warn('[push] registration error', err)
    })

    await PushNotifications.register()
  } catch (err) {
    console.warn('[push] init failed', err)
  }
}
