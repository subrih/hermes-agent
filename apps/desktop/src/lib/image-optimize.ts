// [kaveri fork] Downscale oversized image data URLs in the browser before they
// go to the gateway. Model providers cap media size (MiniMax rejects media
// >10MB; Anthropic ~5MB), and the gateway venv has no Pillow, so we resize
// client-side via <canvas>. Prefer PNG to keep UI/text screenshots crisp, fall
// back to JPEG when PNG is still too big. Works in both Electron and WKWebView.

const DEFAULT_MAX_EDGE = 2000
const DEFAULT_TARGET_BYTES = 4 * 1024 * 1024

/** Approximate decoded byte size of a base64 data URL. */
function dataUrlByteLength(dataUrl: string): number {
  const comma = dataUrl.indexOf(',')
  const b64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl

  return Math.floor((b64.length * 3) / 4)
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()

    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('image decode failed'))
    img.src = src
  })
}

/**
 * Return a data URL that fits within `maxEdge` px on its long side and roughly
 * under `targetBytes`. Returns the input untouched when it's already small
 * enough or isn't a decodable image data URL (best-effort — never throws).
 */
export async function optimizeImageDataUrl(
  dataUrl: string,
  maxEdge: number = DEFAULT_MAX_EDGE,
  targetBytes: number = DEFAULT_TARGET_BYTES
): Promise<string> {
  if (!dataUrl.startsWith('data:image/')) {
    return dataUrl
  }

  try {
    const img = await loadImage(dataUrl)
    const w = img.naturalWidth || img.width
    const h = img.naturalHeight || img.height
    const longEdge = Math.max(w, h)

    if (!longEdge) {
      return dataUrl
    }
    if (longEdge <= maxEdge && dataUrlByteLength(dataUrl) <= targetBytes) {
      return dataUrl
    }

    const scale = longEdge > maxEdge ? maxEdge / longEdge : 1
    const cw = Math.max(1, Math.round(w * scale))
    const ch = Math.max(1, Math.round(h * scale))
    const canvas = document.createElement('canvas')

    canvas.width = cw
    canvas.height = ch
    const ctx = canvas.getContext('2d')

    if (!ctx) {
      return dataUrl
    }
    ctx.drawImage(img, 0, 0, cw, ch)

    // PNG first (lossless — keeps text/UI crisp); JPEG fallback if still large.
    const png = canvas.toDataURL('image/png')

    if (dataUrlByteLength(png) <= targetBytes) {
      return png
    }

    return canvas.toDataURL('image/jpeg', 0.85)
  } catch {
    return dataUrl
  }
}
