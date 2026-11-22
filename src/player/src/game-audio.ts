// The game's sound, on this phone.
//
// It arrives over the game socket as mono 16-bit PCM, about one game frame
// (16 ms) per message, at the rate /api/status names. It is played through Web
// Audio, which the film sound in audio.ts deliberately avoids. The reason it is
// right here: a film's sound is a file the phone can buffer for a minute and
// steer against a clock, but a game's sound is made live and has to be heard
// within a tenth of a second of the button that made it. Only a buffer this
// page fills and drains itself can be that short.
//
// THE BUFFER, AND THE TWO CLOCKS
//
// The stick makes samples by its clock and the phone plays them by its own,
// and the two disagree by a few hundred parts per million. Left alone, the
// buffer would slowly empty (a click and a gap) or slowly fill (the sound
// drifting late behind the picture). So JitterBuffer aims to hold TARGET_S of
// sound, and plays very slightly faster when it holds more than that and
// slower when it holds less -- never more than MAX_SKEW, which no ear hears. A
// burst after a Wi-Fi stall that leaves it past MAX_S is cut back to the target
// at once, because late sound is worse than missing sound.
//
// THE MUTE SWITCH
//
// iOS silences Web Audio when the ring/silent switch is on, which on a child's
// phone it usually is. Safari 17 added navigator.audioSession; setting its type
// to "playback" is what a music app does, and the switch then leaves the game's
// sound alone. Older iOS has no way round it from a web page.

export const TARGET_S = 0.06;
export const MAX_S = 0.25;
export const MAX_SKEW = 0.005;

export class JitterBuffer {
  private ring: Float32Array;
  private head = 0; // next sample to read
  private size = 0; // samples held
  private pos = 0; // fractional read position past head
  private priming = true;
  /** Underruns and overflows since start, for the debug line. */
  underruns = 0;
  trimmed = 0;

  constructor(readonly rate: number) {
    this.ring = new Float32Array(Math.ceil(rate * 1.0));
  }

  /** Seconds of sound held. */
  get held(): number {
    return this.size / this.rate;
  }

  push(pcm: ArrayBuffer): void {
    const view = new DataView(pcm);
    const n = pcm.byteLength >> 1;
    const cap = this.ring.length;
    for (let i = 0; i < n; i++) {
      if (this.size === cap) {
        this.head = (this.head + 1) % cap;
        this.size--;
      }
      this.ring[(this.head + this.size) % cap] = view.getInt16(i * 2, true) / 32768;
      this.size++;
    }
    if (this.held > MAX_S) {
      const drop = this.size - Math.round(TARGET_S * this.rate);
      this.head = (this.head + drop) % cap;
      this.size -= drop;
      this.trimmed++;
    }
  }

  /** Fill `out` with sound for a device running at `outRate`. */
  pull(out: Float32Array, outRate: number): void {
    if (this.priming) {
      if (this.held < TARGET_S) {
        out.fill(0);
        return;
      }
      this.priming = false;
    }
    const error = (this.held - TARGET_S) / TARGET_S;
    const skew = Math.max(-MAX_SKEW, Math.min(MAX_SKEW, error * MAX_SKEW));
    const step = (this.rate / outRate) * (1 + skew);
    const cap = this.ring.length;
    for (let i = 0; i < out.length; i++) {
      if (this.size < 2) {
        // Ran dry: silence for the rest, and wait to refill before playing on.
        out.fill(0, i);
        this.priming = true;
        this.underruns++;
        return;
      }
      const a = this.ring[this.head]!;
      const b = this.ring[(this.head + 1) % cap]!;
      out[i] = a + (b - a) * this.pos;
      this.pos += step;
      while (this.pos >= 1 && this.size >= 2) {
        this.pos -= 1;
        this.head = (this.head + 1) % cap;
        this.size--;
      }
    }
  }
}

/** The bits of the browser this needs, injected for the tests. */
export interface AudioDeps {
  makeContext: () => AudioContextLike | null;
  /** navigator.audioSession, where there is one. */
  session?: { type: string } | null;
}

export interface AudioContextLike {
  sampleRate: number;
  state: string;
  destination: unknown;
  resume(): Promise<void>;
  suspend(): Promise<void>;
  createScriptProcessor(size: number, inputs: number, outputs: number): {
    onaudioprocess: ((ev: { outputBuffer: { getChannelData(n: number): Float32Array } }) => void) | null;
    connect(dest: unknown): void;
    disconnect(): void;
  };
}

// A ScriptProcessor rather than an AudioWorklet. It runs on the page's own
// thread, which an AudioWorklet would not, but this page does almost nothing
// else while a game is up, and it means the buffer above is one class that the
// tests drive directly instead of a second copy inside a worklet's source text.
// 1024 frames is 21 ms at 48 kHz: about one game frame.
const BLOCK = 1024;

export class GameAudio {
  private ctx: AudioContextLike | null = null;
  private node: ReturnType<AudioContextLike["createScriptProcessor"]> | null = null;
  private buffer: JitterBuffer | null = null;
  private _on = false;

  constructor(private readonly deps: AudioDeps) {}

  get on(): boolean {
    return this._on;
  }

  get buffered(): JitterBuffer | null {
    return this.buffer;
  }

  /** Called from a tap: the only moment a phone lets a page start sound. */
  unlock(): void {
    if (this.deps.session) {
      try {
        this.deps.session.type = "playback";
      } catch {
        /* an older Safari: the mute switch wins */
      }
    }
    if (!this.ctx) {
      this.ctx = this.deps.makeContext();
    }
    if (this.ctx && this.ctx.state !== "running") {
      void this.ctx.resume();
    }
  }

  /** Start playing a stream at `rate`. Idempotent for the same rate. */
  start(rate: number): void {
    if (!this.ctx) {
      return;
    }
    if (this.buffer && this.buffer.rate === rate && this.node) {
      return;
    }
    this.stop();
    const ctx = this.ctx;
    const buffer = new JitterBuffer(rate);
    const node = ctx.createScriptProcessor(BLOCK, 0, 1);
    node.onaudioprocess = (ev) => {
      buffer.pull(ev.outputBuffer.getChannelData(0), ctx.sampleRate);
    };
    node.connect(ctx.destination);
    this.buffer = buffer;
    this.node = node;
    this._on = true;
  }

  push(pcm: ArrayBuffer): void {
    this.buffer?.push(pcm);
  }

  stop(): void {
    if (this.node) {
      this.node.onaudioprocess = null;
      this.node.disconnect();
    }
    this.node = null;
    this.buffer = null;
    this._on = false;
  }
}
