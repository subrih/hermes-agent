// [kaveri fork] Over-the-air renderer-bundle updates for the packaged desktop
// app — the Electron twin of apps/desktop/src/platform/live-update.ts (which
// does this on iOS via Capgo). It reads the SAME OTA channel + version.json the
// iPhone uses, so a single `publish` updates both Mac and iPhone.
//
// Flow: main loads the newest CACHED bundle (or the app-bundled dist if none),
// then in the background checks the channel; if a newer web bundle exists it
// downloads + extracts it under userData and reloads the window into it. The
// preload bridge is attached by absolute path, so it survives the renderer swap;
// vite base is './', so the bundle loads correctly from any directory.
// Only runs in the packaged app (never dev). Zero-dependency (node + /usr/bin/unzip).

const fs = require('node:fs')
const path = require('node:path')
const https = require('node:https')
const { spawnSync } = require('node:child_process')
const { pathToFileURL } = require('node:url')
const { app } = require('electron')

const CHANNEL = 'https://ota.hellopulse.ai/a3301684eb130bc7a0e7da61'

function cacheRoot() {
  return path.join(app.getPath('userData'), 'web-bundles')
}

function readState() {
  try {
    return JSON.parse(fs.readFileSync(path.join(cacheRoot(), 'state.json'), 'utf8'))
  } catch {
    return {}
  }
}

function writeState(state) {
  fs.mkdirSync(cacheRoot(), { recursive: true })
  fs.writeFileSync(path.join(cacheRoot(), 'state.json'), JSON.stringify(state))
}

// Absolute path to the active cached bundle's index.html, or null if none valid.
// main.cjs prefers this over the app-bundled dist when present.
function activeRendererIndex() {
  try {
    const { version } = readState()
    if (!version) return null
    const idx = path.join(cacheRoot(), version, 'index.html')
    return fs.existsSync(idx) ? idx : null
  } catch {
    return null
  }
}

function fetchText(url, timeoutMs = 6000) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, res => {
      if (res.statusCode !== 200) {
        res.resume()
        reject(new Error(`status ${res.statusCode}`))
        return
      }
      let data = ''
      res.setEncoding('utf8')
      res.on('data', chunk => (data += chunk))
      res.on('end', () => resolve(data))
    })
    req.on('error', reject)
    req.setTimeout(timeoutMs, () => req.destroy(new Error('timeout')))
  })
}

function download(url, dest, timeoutMs = 90000) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest)
    const req = https.get(url, res => {
      if (res.statusCode !== 200) {
        res.resume()
        file.close()
        fs.rm(dest, { force: true }, () => reject(new Error(`status ${res.statusCode}`)))
        return
      }
      res.pipe(file)
      file.on('finish', () => file.close(err => (err ? reject(err) : resolve())))
    })
    req.on('error', err => fs.rm(dest, { force: true }, () => reject(err)))
    req.setTimeout(timeoutMs, () => req.destroy(new Error('timeout')))
  })
}

// Check the channel and, if a newer bundle exists, download + extract it and
// reload the window into it. Best-effort: any failure leaves the current bundle
// untouched. `log` is an optional progress sink (main.cjs pipes it to its log).
async function runWebUpdate(win, log = () => {}) {
  try {
    const latest = JSON.parse(await fetchText(`${CHANNEL}/version.json`))
    if (!latest || !latest.version || !latest.url) return
    if (readState().version === latest.version && activeRendererIndex()) {
      log(`up to date ${latest.version}`)
      return
    }

    fs.mkdirSync(cacheRoot(), { recursive: true })
    const destDir = path.join(cacheRoot(), latest.version)
    const tmpZip = path.join(cacheRoot(), `${latest.version}.zip`)
    log(`downloading ${latest.version}`)
    await download(latest.url, tmpZip)

    fs.rmSync(destDir, { recursive: true, force: true })
    fs.mkdirSync(destDir, { recursive: true })
    const unzip = spawnSync('/usr/bin/unzip', ['-o', '-q', tmpZip, '-d', destDir])
    fs.rmSync(tmpZip, { force: true })
    if (unzip.status !== 0) {
      throw new Error(`unzip failed: ${unzip.stderr ? unzip.stderr.toString() : unzip.status}`)
    }
    const idx = path.join(destDir, 'index.html')
    if (!fs.existsSync(idx)) throw new Error('no index.html in downloaded bundle')

    writeState({ version: latest.version })
    log(`applied ${latest.version} (reloading)`)
    if (win && !win.isDestroyed()) win.loadURL(pathToFileURL(idx).toString())
  } catch (err) {
    log(`error: ${err && err.message ? err.message : String(err)}`)
  }
}

module.exports = { activeRendererIndex, runWebUpdate }
