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

// [kaveri fork] First-run default so the device build connects out of the box.
// TODO(phase2): move these to on-device entry stored in the iOS Keychain
// instead of baking them into the binary (this IPA is personal/ad-hoc only).
const DEFAULT_CONFIG: StoredConfig = {
  mode: 'remote',
  remoteUrl: 'https://kav.hellopulse.ai',
  token: 'Z2JcYvCjnThi-mC4hWfG_XtAZggsZq1UapUGT9p9HOs',
  cfAccessId: '857150541c167b0a4edeccd94391515b.access',
  cfAccessSecret: 'c473ae93ab91296463af1326433f3cf70a6fa79c53c1e11a60b8f507c9651cc2'
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
    getConnection: async (profile?: string | null) => buildConnection(await loadConfig(), profile),
    getGatewayWsUrl: async (profile?: string | null) => wsUrlFor(await loadConfig(), profile),
    touchBackend: async () => ({ ok: true }),
    getBootProgress: async () => ({ error: null, fakeMode: false, message: '', phase: 'ready', progress: 100, running: false, timestamp: Date.now() }),
    getConnectionConfig: async () => {
      const c = await loadConfig()
      return {
        envOverride: false,
        mode: c.mode,
        remoteAuthMode: 'token' as const,
        remoteOauthConnected: false,
        remoteTokenPreview: c.token ? `...${c.token.slice(-6)}` : null,
        remoteTokenSet: Boolean(c.token),
        remoteUrl: c.remoteUrl
      }
    },
    saveConnectionConfig: async (p: any) => {
      const c = await loadConfig()
      await saveConfig({ ...c, mode: p.mode ?? c.mode, remoteUrl: p.remoteUrl ?? c.remoteUrl, token: p.remoteToken || c.token })
      return (await (window as any).hermesDesktop.getConnectionConfig())
    },
    applyConnectionConfig: async (p: any) => {
      const c = await loadConfig()
      await saveConfig({ ...c, mode: p.mode ?? c.mode, remoteUrl: p.remoteUrl ?? c.remoteUrl, token: p.remoteToken || c.token })
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
        await Preferences.set({ key: PROFILE_KEY, value: name ?? '' })
        setTimeout(() => window.location.reload(), 150)
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

export function isCapacitorIos(): boolean {
  try {
    return Capacitor.isNativePlatform()
  } catch {
    return false
  }
}

export async function initIosBridge(): Promise<void> {
  if (!isCapacitorIos()) return
  // Native socket factory for the gateway client (apps/shared json-rpc-gateway
  // picks this up via HermesGateway's ctor — see hermes.ts [kaveri fork]).
  ;(window as any).__hermesNativeSocketFactory = (url: string) => {
    const cfg = cachedConfig ?? { mode: 'remote', remoteUrl: '', token: '' }
    return new NativeWebSocket(url, cfHeaders(cfg)) as unknown as WebSocket
  }
  await loadConfig()
  ;(window as any).hermesDesktop = buildBridge()
}
