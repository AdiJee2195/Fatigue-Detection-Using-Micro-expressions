/// <reference types="vite/client" />

/**
 * Build-time configuration.
 *
 * Declaring these means a typo in the code (or in a `.env`) is a compile error
 * rather than a silently-ignored variable. Everything here has a runtime default,
 * so the app runs with no `.env` at all — which is the point: `npm run dev` must
 * work in a fresh clone.
 */
interface ImportMetaEnv {
  /** Gateway WebSocket base, e.g. `ws://localhost:8080`. */
  readonly VITE_GATEWAY_WS?: string;
  /** Default session id. Precedence: `?session=` > remembered id > this > generated. */
  readonly VITE_SESSION_ID?: string;
  /** Target capture rate in frames per second. */
  readonly VITE_CAPTURE_FPS?: string;
  /** Frames are downscaled to this width before encoding. */
  readonly VITE_CAPTURE_MAX_WIDTH?: string;
  /** JPEG quality, 0-1. */
  readonly VITE_JPEG_QUALITY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
