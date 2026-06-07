// [kaveri fork] iOS bridge — re-creates the `window.hermesDesktop` surface for
// the Capacitor (iPhone) build. The desktop renderer talks to the backend only
// through `window.hermesDesktop` (normally the Electron preload IPC bridge);
// under Capacitor there is no Electron, so we provide it natively:
//   - REST  → CapacitorHttp (native URLSession; sets X-Hermes-Session-Token +
//             CF-Access-Client-Id/Secret — a native client CAN set headers).
//   - WS    → a native URLSessionWebSocketTask (HermesWS plugin) that sets the
//             same headers on the upgrade, exposed as a WebSocketLike and wired
//             through the gateway client's `socketFactory` seam.
//   - config/secrets → @capacitor/preferences (Keychain-backed on iOS).
// Desktop builds never import this (guarded by Capacitor.isNativePlatform()).

import { Capacitor, CapacitorHttp, registerPlugin } from '@capacitor/core'
import { Preferences } from '@capacitor/preferences'

const CONFIG_KEY = 'hermes.connection'
const PROFILE_KEY = 'hermes.activeProfile'

interface StoredConfig {
  mode: 'local' | 'remote'
  remoteUrl: string
  token: string
  cfAccessId?: string
  cfAccessSecret?: string
}

let cachedConfig: StoredConfig | null = null

// [kaveri fork] First-run default. Secrets are NO LONGER baked in — only the
// (non-secret) gateway URL is pre-filled for convenience. With no token the app
// boots into the connection-setup path (getConnection throws → boot-failure
// overlay → "Open settings"), where the user enters the token + CF Access creds
// once; they're then stored on-device (@capacitor/preferences). Existing
// installs are unaffected: push-1's auto-migrate already persisted their creds.
const DEFAULT_CONFIG: StoredConfig = {
  mode: 'remote',
  remoteUrl: 'https://kav.hellopulse.ai',
  token: ''
}

async function loadConfig(): Promise<StoredConfig> {
  if (cachedConfig) return cachedConfig
  const { value } = await Preferences.get({ key: CONFIG_KEY })
  cachedConfig = value ? (JSON.parse(value) as StoredConfig) : { ...DEFAULT_CONFIG }
  return cachedConfig
}

async function saveConfig(next: StoredConfig): Promise<void> {
  cachedConfig = next
  await Preferences.set({ key: CONFIG_KEY, value: JSON.stringify(next) })
}

// Merge a settings-UI payload onto the stored config. Token + CF secret are
// write-only: a blank value keeps the saved one (so re-saving the form without
// re-typing secrets doesn't wipe them). cfAccessId is non-secret → always set.
async function persistConfigInput(p: {
  mode?: 'local' | 'remote'
  remoteUrl?: string
  remoteToken?: string
  cfAccessId?: string
  cfAccessSecret?: string
}): Promise<void> {
  const c = await loadConfig()
  await saveConfig({
    ...c,
    mode: p.mode ?? c.mode,
    remoteUrl: p.remoteUrl ?? c.remoteUrl,
    token: p.remoteToken || c.token,
    cfAccessId: p.cfAccessId ?? c.cfAccessId,
    cfAccessSecret: p.cfAccessSecret || c.cfAccessSecret
  })
}

function cfHeaders(cfg: StoredConfig): Record<string, string> {
  if (cfg.cfAccessId && cfg.cfAccessSecret) {
    return {
      'CF-Access-Client-Id': cfg.cfAccessId,
      'CF-Access-Client-Secret': cfg.cfAccessSecret
    }
  }
  return {}
}

function normBase(url: string): string {
  return url.replace(/\/+$/, '')
}

function wsUrlFor(cfg: StoredConfig, profile?: string | null): string {
  const base = normBase(cfg.remoteUrl)
  const ws = base.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:')
  const params = new URLSearchParams({ token: cfg.token })
  if (profile) params.set('profile', profile)
  return `${ws}/api/ws?${params.toString()}`
}

function buildConnection(cfg: StoredConfig, profile?: string | null) {
  return {
    baseUrl: normBase(cfg.remoteUrl),
    token: cfg.token,
    wsUrl: wsUrlFor(cfg, profile),
    mode: 'remote' as const,
    authMode: 'token' as const,
    source: 'settings' as const,
    profile: profile ?? undefined,
    isFullscreen: false,
    nativeOverlayWidth: 0,
    windowButtonPosition: null,
    logs: [] as string[]
  }
}

// --- REST via CapacitorHttp -------------------------------------------------
async function apiRequest<T>(request: {
  path: string
  method?: string
  body?: unknown
  timeoutMs?: number
  profile?: string | null
}): Promise<T> {
  const cfg = await loadConfig()
  // [kaveri fork] Unconfigured: fail fast. Without a token (and CF Access creds)
  // a request to a CF-gated host returns the Cloudflare Access *login page* with
  // status 200 — which would otherwise be parsed as data and poison stores
  // (e.g. $profiles.set(undefined) → ChatSidebar crash). Throwing keeps the
  // best-effort store refreshers on their cached values until configured.
  if (!cfg.token) {
    throw new Error('No gateway configured. Open Settings → Gateway to enter your connection details.')
  }
  const url = `${normBase(cfg.remoteUrl)}${request.path}`
  const res = await CapacitorHttp.request({
    url,
    method: request.method || 'GET',
    headers: {
      'Content-Type': 'application/json',
      'X-Hermes-Session-Token': cfg.token,
      ...cfHeaders(cfg)
    },
    data: request.body,
    connectTimeout: request.timeoutMs,
    readTimeout: request.timeoutMs
  })
  if (res.status >= 400) {
    throw new Error(`${res.status}: ${typeof res.data === 'string' ? res.data : JSON.stringify(res.data)}`)
  }
  return res.data as T
}

// --- Native WebSocket (HermesWS plugin) wired as WebSocketLike --------------
interface HermesWSPlugin {
  connect(opts: { url: string; headers: Record<string, string> }): Promise<{ id: number }>
  send(opts: { id: number; data: string }): Promise<void>
  close(opts: { id: number }): Promise<void>
  addListener(
    event: 'wsEvent',
    cb: (e: { id: number; type: 'open' | 'message' | 'close' | 'error'; data?: string }) => void
  ): Promise<{ remove: () => void }>
}

const HermesWS = registerPlugin<HermesWSPlugin>('HermesWS')

type Listener = (ev: any) => void

// Minimal WebSocket-compatible wrapper backed by the native plugin. The shared
// gateway client (apps/shared/src/json-rpc-gateway.ts) only uses:
//   addEventListener('open'|'message'|'close'|'error'), send(), close(),
//   readyState (=== WebSocket.OPEN).
class NativeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readyState = 0
  private id: number | null = null
  private listeners: Record<string, Set<Listener>> = { open: new Set(), message: new Set(), close: new Set(), error: new Set() }
  private removeNative: (() => void) | null = null
  private outbox: string[] = []

  constructor(url: string, headers: Record<string, string>) {
    void this.open(url, headers)
  }

  private emit(type: string, ev: any) {
    for (const l of this.listeners[type] ?? []) l(ev)
  }

  private async open(url: string, headers: Record<string, string>) {
    const sub = await HermesWS.addListener('wsEvent', e => {
      if (e.id !== this.id) return
      if (e.type === 'open') {
        this.readyState = 1
        for (const m of this.outbox.splice(0)) void HermesWS.send({ id: this.id!, data: m })
        this.emit('open', { type: 'open' })
      } else if (e.type === 'message') {
        this.emit('message', { type: 'message', data: e.data })
      } else if (e.type === 'close') {
        this.readyState = 3
        this.emit('close', { type: 'close' })
      } else if (e.type === 'error') {
        this.emit('error', { type: 'error', message: e.data })
      }
    })
    this.removeNative = () => void sub.remove()
    try {
      const { id } = await HermesWS.connect({ url, headers })
      this.id = id
    } catch (err) {
      this.readyState = 3
      this.emit('error', { type: 'error', message: String(err) })
      this.emit('close', { type: 'close' })
    }
  }

  send(data: string) {
    if (this.id == null || this.readyState !== 1) {
      this.outbox.push(data)
      return
    }
    void HermesWS.send({ id: this.id, data })
  }

  close() {
    this.readyState = 2
    if (this.id != null) void HermesWS.close({ id: this.id })
    this.removeNative?.()
  }

  addEventListener(type: string, handler: Listener) {
    this.listeners[type]?.add(handler)
  }

  removeEventListener(type: string, handler: Listener) {
    this.listeners[type]?.delete(handler)
  }
}

// --- stubs for desktop-only surface (renderer guards most with ?.) ----------
const noopUnsub = () => () => {}
const rejectUnsupported = () => Promise.reject(new Error('Not available on iOS'))

function buildBridge() {
  return {
    getConnection: async (profile?: string | null) => {
      const cfg = await loadConfig()
      // [kaveri fork] Unconfigured (secrets de-baked): fail boot deliberately so
      // the boot-failure overlay shows its "Open settings" path instead of
      // silently spinning on a doomed, token-less connection.
      if (!cfg.token) {
        throw new Error(
          'No gateway configured. Open Settings → Gateway to enter your remote URL, session token, and Cloudflare Access credentials.'
        )
      }
      return buildConnection(cfg, profile)
    },
    getGatewayWsUrl: async (profile?: string | null) => wsUrlFor(await loadConfig(), profile),
    touchBackend: async () => ({ ok: true }),
    getBootProgress: async () => ({ error: null, fakeMode: false, message: '', phase: 'ready', progress: 100, running: false, timestamp: Date.now() }),
    getConnectionConfig: async () => {
      const c = await loadConfig()
      return {
        envOverride: false,
        // [kaveri fork] Report the EFFECTIVE mode, not the raw stored field.
        // buildConnection() always connects remotely (gating on token), so a
        // device that stored mode:'local' from an early build would otherwise
        // show "local" + hide the CF fields forever even though it's working
        // remotely. Derive remote whenever a usable remote+token is configured.
        mode: c.remoteUrl && c.token ? 'remote' : c.mode,
        remoteAuthMode: 'token' as const,
        remoteOauthConnected: false,
        remoteTokenPreview: c.token ? `...${c.token.slice(-6)}` : null,
        remoteTokenSet: Boolean(c.token),
        remoteUrl: c.remoteUrl,
        // [kaveri fork] CF Access fields — drive the iOS-only settings inputs.
        cfAccessSupported: true,
        cfAccessId: c.cfAccessId ?? '',
        cfAccessSecretSet: Boolean(c.cfAccessSecret),
        cfAccessSecretPreview: c.cfAccessSecret ? `...${c.cfAccessSecret.slice(-6)}` : null
      }
    },
    saveConnectionConfig: async (p: any) => {
      await persistConfigInput(p)
      return (await (window as any).hermesDesktop.getConnectionConfig())
    },
    applyConnectionConfig: async (p: any) => {
      await persistConfigInput(p)
      setTimeout(() => window.location.reload(), 150)
      return (await (window as any).hermesDesktop.getConnectionConfig())
    },
    testConnectionConfig: async () => {
      const status = await apiRequest<any>({ path: '/api/status' })
      const c = await loadConfig()
      return { baseUrl: normBase(c.remoteUrl), ok: true, version: status?.version ?? null }
    },
    probeConnectionConfig: async (remoteUrl: string) => ({ baseUrl: normBase(remoteUrl), reachable: true, authMode: 'token' as const, providers: [], version: null, error: null }),
    oauthLoginConnectionConfig: rejectUnsupported,
    oauthLogoutConnectionConfig: rejectUnsupported,
    profile: {
      get: async () => ({ profile: (await Preferences.get({ key: PROFILE_KEY })).value ?? null }),
      set: async (name: string | null) => {
        // [kaveri fork] Just persist the preference — do NOT reload. The desktop
        // never reloads on a profile switch; the renderer swaps the gateway
        // in-place (ensureGatewayProfile → getConnection(profile)) when you next
        // send/open in that profile. Reloading here threw away that swap (boots
        // back to the primary/cockpit) and slammed the mobile drawer shut.
        await Preferences.set({ key: PROFILE_KEY, value: name ?? '' })
        return { profile: name }
      }
    },
    api: <T>(request: any) => apiRequest<T>(request),
    notify: async () => true,
    requestMicrophoneAccess: async () => true,
    // file/preview/clipboard — Phase 2; safe stubs for now
    readFileDataUrl: rejectUnsupported,
    readFileText: rejectUnsupported,
    selectPaths: async () => [],
    writeClipboard: async (text: string) => { try { await navigator.clipboard.writeText(text); return true } catch { return false } },
    saveImageFromUrl: rejectUnsupported,
    saveImageBuffer: rejectUnsupported,
    saveClipboardImage: rejectUnsupported,
    getPathForFile: () => '',
    normalizePreviewTarget: async () => null,
    watchPreviewFile: rejectUnsupported,
    stopPreviewFileWatch: async () => false,
    onPreviewFileChanged: noopUnsub,
    openExternal: async (url: string) => { window.open(url, '_blank') },
    fetchLinkTitle: rejectUnsupported,
    settings: {
      getDefaultProjectDir: async () => ({ defaultLabel: '', dir: null }),
      setDefaultProjectDir: async (dir: string | null) => ({ dir }),
      pickDefaultProjectDir: async () => ({ canceled: true, dir: null })
    },
    revealLogs: async () => ({ ok: false, path: '', error: 'unsupported' }),
    getRecentLogs: async () => ({ path: '', lines: [] }),
    readDir: async () => ({ entries: [], error: 'unsupported' }),
    gitRoot: async () => null,
    terminal: {
      dispose: async () => false,
      onData: noopUnsub,
      onExit: noopUnsub,
      resize: async () => false,
      start: rejectUnsupported,
      write: async () => false
    },
    onClosePreviewRequested: noopUnsub,
    onOpenUpdatesRequested: noopUnsub,
    onWindowStateChanged: noopUnsub,
    onPreviewFileChanged2: noopUnsub,
    onBackendExit: noopUnsub,
    onPowerResume: noopUnsub,
    onBootProgress: noopUnsub,
    getBootstrapState: async () => ({ active: false, manifest: null, stages: {}, error: null, log: [], startedAt: null, completedAt: null, unsupportedPlatform: null }),
    resetBootstrap: async () => ({ ok: true }),
    repairBootstrap: async () => ({ ok: true }),
    cancelBootstrap: async () => ({ ok: true, cancelled: false }),
    onBootstrapEvent: noopUnsub,
    getVersion: async () => ({ appVersion: '0.16.0', electronVersion: '', nodeVersion: '', platform: 'ios', hermesRoot: '' }),
    updates: {
      check: async () => ({ supported: false }),
      apply: async () => ({ ok: false }),
      getBranch: async () => ({ branch: 'main' }),
      setBranch: async (name: string) => ({ branch: name }),
      onProgress: noopUnsub
    }
  }
}

// [kaveri fork] Self-contained connection-setup overlay for a fresh (unconfigured)
// iOS install. Secrets are no longer baked in, so a clean install has no token.
// Rather than route through the app's boot/onboarding overlays (which collide on
// iOS), we paint our own full-screen form ABOVE everything (max z-index). On save
// we persist to Preferences and reload; the app then boots normally. The saved
// URL + CF id are pre-filled; secrets are entered fresh.
function showSetupForm(cfg: StoredConfig): void {
  if (document.getElementById('hermes-ios-setup')) return
  const wrap = document.createElement('div')
  wrap.id = 'hermes-ios-setup'
  wrap.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;background:#0b0f1a;color:#e7ecf3;' +
    'font:14px -apple-system,system-ui,sans-serif;overflow:auto;' +
    'padding:max(48px,env(safe-area-inset-top)) 20px calc(40px + env(safe-area-inset-bottom));'
  const field = (label: string, id: string, type: string, placeholder: string) =>
    `<label style="display:block;margin:0 0 14px">
       <div style="font-size:12px;color:#9aa7bd;margin:0 0 6px">${label}</div>
       <input id="${id}" type="${type}" placeholder="${placeholder}" autocapitalize="off" autocorrect="off" spellcheck="false"
         style="width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #2a3550;border-radius:10px;background:#121a2b;color:#e7ecf3;font:14px ui-monospace,monospace" />
     </label>`
  wrap.innerHTML =
    `<div style="max-width:480px;margin:0 auto">
       <h1 style="font-size:20px;font-weight:600;margin:0 0 6px">Connect to Kaveri</h1>
       <p style="font-size:13px;color:#9aa7bd;margin:0 0 22px;line-height:1.5">Enter your gateway URL, session token, and Cloudflare Access service token. Stored only on this device.</p>
       ${field('Gateway URL', 'f-url', 'url', 'https://kav.hellopulse.ai')}
       ${field('Session token', 'f-token', 'password', 'dashboard session token')}
       ${field('CF Access client ID', 'f-cfid', 'text', 'xxxx.access')}
       ${field('CF Access client secret', 'f-cfsecret', 'password', 'client secret')}
       <button id="f-save" style="width:100%;margin-top:8px;padding:13px;border:0;border-radius:10px;background:#2f6df6;color:#fff;font-size:15px;font-weight:600">Connect</button>
       <div id="f-err" style="color:#ff8a8a;font-size:12px;margin-top:10px;min-height:14px"></div>
     </div>`
  document.body.appendChild(wrap)
  // Pre-fill non-secret saved values via .value (avoids HTML-injection).
  ;(document.getElementById('f-url') as HTMLInputElement).value = cfg.remoteUrl || ''
  ;(document.getElementById('f-cfid') as HTMLInputElement).value = cfg.cfAccessId || ''
  const val = (id: string) => (document.getElementById(id) as HTMLInputElement | null)?.value.trim() ?? ''
  document.getElementById('f-save')?.addEventListener('click', () => {
    const err = document.getElementById('f-err')!
    const remoteUrl = val('f-url')
    const remoteToken = val('f-token')
    if (!remoteUrl || !remoteToken) {
      err.textContent = 'Gateway URL and session token are required.'
      return
    }
    void persistConfigInput({
      mode: 'remote',
      remoteUrl,
      remoteToken,
      cfAccessId: val('f-cfid'),
      cfAccessSecret: val('f-cfsecret')
    })
      .then(() => window.location.reload())
      .catch(e => {
        err.textContent = String(e)
      })
  })
}

export function isCapacitorIos(): boolean {
  try {
    return Capacitor.isNativePlatform()
  } catch {
    return false
  }
}

export async function initIosBridge(): Promise<void> {
  if (!isCapacitorIos()) return
  // [kaveri fork] Tag the root so the iOS responsive stylesheet (mobile-ios.css)
  // can scope all its overrides under [data-platform="ios"] — desktop untouched.
  document.documentElement.dataset.platform = 'ios'
  // Native socket factory for the gateway client (apps/shared json-rpc-gateway
  // picks this up via HermesGateway's ctor — see hermes.ts [kaveri fork]).
  ;(window as any).__hermesNativeSocketFactory = (url: string) => {
    const cfg = cachedConfig ?? { mode: 'remote', remoteUrl: '', token: '' }
    return new NativeWebSocket(url, cfHeaders(cfg)) as unknown as WebSocket
  }
  // [kaveri fork] One-time migration: if nothing is stored yet, persist the
  // resolved config (the baked default) into Preferences. This lets a later
  // bundle drop the baked secrets without stranding an already-installed device
  // — its creds now live on-device, not only in the shipped bundle.
  const stored = await Preferences.get({ key: CONFIG_KEY })
  await loadConfig()
  if (!stored.value && cachedConfig) await saveConfig(cachedConfig)
  ;(window as any).hermesDesktop = buildBridge()
  // [kaveri fork] No token yet (fresh install, secrets de-baked) → paint the
  // setup form over the app. The app still boots underneath (harmless: api
  // calls fail fast without a token), but this overlay sits above everything.
  if (!cachedConfig?.token) showSetupForm(cachedConfig ?? DEFAULT_CONFIG)
}
