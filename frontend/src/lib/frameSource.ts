/**
 * Frame source: the camera, or an explicitly-labelled synthetic placeholder.
 *
 * The hard rule this module exists to enforce: **never fake a camera feed**. If the
 * camera is unavailable, denied or not yet open, the app does not invent imagery.
 * It either sends a blank placeholder frame (clearly described as carrying no image
 * data) or it sends nothing at all.
 *
 * Why a placeholder frame is acceptable in demo mode: the vision service's stub
 * pipeline ignores frame content by design — no decoder runs, no pixels are read —
 * so the frame is a transport-level formality, not data. The dashboard says so.
 * In ONNX mode a placeholder frame can never produce meaningful telemetry, which is
 * exactly why the mode badge distinguishes the two.
 */

export const DEMO_FRAME_PX = 16;

export type CameraStatus =
  | 'unavailable'
  | 'idle'
  | 'requesting'
  | 'live'
  | 'denied'
  | 'error';

export interface FrameCaptureOptions {
  /** Downscaled width in CSS pixels. Full resolution costs more than it buys. */
  maxWidth: number;
  /** JPEG quality, 0-1. */
  jpegQuality: number;
}

export const DEFAULT_CAPTURE: FrameCaptureOptions = { maxWidth: 640, jpegQuality: 0.6 };

/** True when the browser can even attempt a camera. `getUserMedia` needs a secure context. */
export function cameraSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    typeof navigator.mediaDevices !== 'undefined' &&
    typeof navigator.mediaDevices.getUserMedia === 'function'
  );
}

let scratchCanvas: HTMLCanvasElement | null = null;

function getScratchCanvas(width = DEMO_FRAME_PX, height = DEMO_FRAME_PX): HTMLCanvasElement | null {
  if (typeof document === 'undefined') return null;
  if (!scratchCanvas) {
    scratchCanvas = document.createElement('canvas');
  }
  scratchCanvas.width = width;
  scratchCanvas.height = height;
  return scratchCanvas;
}

/**
 * A blank, uniform JPEG — 16x16, no image content.
 *
 * Used only when the demo source is selected and no camera is in use. It contains
 * nothing: no face, no scene, no identifiable information. Stub mode never decodes it.
 */
export function createPlaceholderFrame(): string | null {
  const canvas = getScratchCanvas();
  if (!canvas) return null;
  const context = canvas.getContext('2d');
  if (!context) return null;
  context.fillStyle = '#000000';
  context.fillRect(0, 0, canvas.width, canvas.height);
  return stripDataUrlPrefix(canvas.toDataURL('image/jpeg', 0.5));
}

/** The wire format takes the base64 payload only — the `data:` prefix must go. */
export function stripDataUrlPrefix(dataUrl: string): string {
  const comma = dataUrl.indexOf(',');
  return comma === -1 ? dataUrl : dataUrl.slice(comma + 1);
}

/**
 * Capture one JPEG frame from a live video element, scaled to `maxWidth`.
 *
 * Returns null when the element has no current frame yet. The caller must treat
 * null as "skip this tick" — never as "send the previous frame", because a late
 * frame is worse than a missing one against this pipeline.
 */
export function captureVideoFrame(
  video: HTMLVideoElement,
  options: FrameCaptureOptions = DEFAULT_CAPTURE,
): string | null {
  // readyState < HAVE_CURRENT_DATA means there is no decoded frame to draw.
  if (video.readyState < 2) return null;
  const sourceWidth = video.videoWidth;
  const sourceHeight = video.videoHeight;
  if (sourceWidth <= 0 || sourceHeight <= 0) return null;

  const scale = Math.min(1, options.maxWidth / sourceWidth);
  const width = Math.max(1, Math.round(sourceWidth * scale));
  const height = Math.max(1, Math.round(sourceHeight * scale));

  const canvas = getScratchCanvas(width, height);
  if (!canvas) return null;
  const context = canvas.getContext('2d');
  if (!context) return null;

  try {
    context.drawImage(video, 0, 0, width, height);
    return stripDataUrlPrefix(canvas.toDataURL('image/jpeg', options.jpegQuality));
  } catch {
    // drawImage throws if the video is tainted or has no frame; skip, do not retry.
    return null;
  }
}

/**
 * Owns the `getUserMedia` lifecycle and the preview stream.
 *
 * The controller is deliberately explicit about failure: `denied` and `error` are
 * different states because they need different messages ("you blocked the camera"
 * versus "the camera is in use by another application").
 */
export class CameraController {
  status: CameraStatus = 'idle';
  message: string | null = null;

  private stream: MediaStream | null = null;
  private video: HTMLVideoElement | null = null;
  private onChange: (() => void) | null = null;
  private requestToken = 0;

  constructor(onChange?: () => void) {
    this.onChange = onChange ?? null;
    if (!cameraSupported()) {
      this.status = 'unavailable';
      this.message = 'This browser or context does not expose a camera API (needs https or localhost).';
    }
  }

  getStream(): MediaStream | null {
    return this.stream;
  }

  /** True when frames can actually be captured right now. */
  get isCapturing(): boolean {
    return this.status === 'live' && this.video !== null && this.stream !== null;
  }

  private notify(): void {
    this.onChange?.();
  }

  async start(): Promise<void> {
    if (!cameraSupported()) {
      this.status = 'unavailable';
      this.notify();
      return;
    }
    if (this.status === 'requesting') return;

    this.status = 'requesting';
    this.message = 'Waiting for camera permission…';
    this.notify();

    const token = ++this.requestToken;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: 'user' },
        audio: false,
      });
      // A newer request superseded this one (user toggled back to demo).
      if (token !== this.requestToken) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      this.attach(stream);
      this.status = 'live';
      this.message = null;
    } catch (error) {
      if (token !== this.requestToken) return;
      const name = error instanceof Error ? error.name : '';
      if (name === 'NotAllowedError' || name === 'SecurityError') {
        this.status = 'denied';
        this.message = 'Camera permission was denied. Enable it in the browser, or use demo mode.';
      } else if (name === 'NotFoundError' || name === 'OverconstrainedError') {
        this.status = 'unavailable';
        this.message = 'No camera device was found. Demo mode is available.';
      } else {
        this.status = 'error';
        this.message = 'The camera could not be started. Demo mode is available.';
      }
    }
    this.notify();
  }

  private attach(stream: MediaStream): void {
    this.stopStream();
    const video = document.createElement('video');
    video.playsInline = true;
    video.muted = true;
    video.autoplay = true;
    video.srcObject = stream;
    this.video = video;
    this.stream = stream;
    // play() rejection is harmless here: capture checks readyState itself.
    void video.play().catch(() => undefined);
  }

  stop(): void {
    this.requestToken += 1;
    this.stopStream();
    if (this.status === 'live' || this.status === 'requesting') {
      this.status = 'idle';
      this.message = null;
    }
    this.notify();
  }

  private stopStream(): void {
    if (this.stream) {
      for (const track of this.stream.getTracks()) {
        try {
          track.stop();
        } catch {
          /* already ended */
        }
      }
    }
    if (this.video) {
      this.video.srcObject = null;
    }
    this.video = null;
    this.stream = null;
  }

  capture(options: FrameCaptureOptions = DEFAULT_CAPTURE): string | null {
    if (!this.video) return null;
    return captureVideoFrame(this.video, options);
  }

  dispose(): void {
    this.stop();
    this.onChange = null;
  }
}
