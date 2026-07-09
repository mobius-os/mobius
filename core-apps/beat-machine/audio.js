export const PADS = 16
export const KIT_PADS = 8
export const CUSTOM_START = 8
export const MAX_RECORD_SECONDS = 6

export const PAD_COLORS = [
  '#a78bfa', '#38bdf8', '#f472b6', '#f59e0b',
  '#34d399', '#818cf8', '#fb7185', '#22d3ee',
  '#c084fc', '#60a5fa', '#f97316', '#10b981',
  '#f43f5e', '#8b5cf6', '#eab308', '#06b6d4',
]

function audioContextCtor() {
  if (typeof window === 'undefined') return null
  return window.AudioContext || window.webkitAudioContext || null
}

export class AudioEngine {
  constructor() {
    this.ctx = null
    this.master = null
    this.compressor = null
    this.padGains = new Array(PADS).fill(null)
    this.volumes = new Array(PADS).fill(0.8)
    this.echoDelay = null
    this.echoFeedback = null
    this.echoWet = null
    this.reverbConvolver = null
    this.reverbWet = null
  }

  init() {
    if (this.ctx) {
      if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {})
      return this.ctx
    }

    const Ctor = audioContextCtor()
    if (!Ctor) throw new Error('Web Audio is unavailable in this browser.')
    this.ctx = new Ctor()

    this.compressor = this.ctx.createDynamicsCompressor()
    this.compressor.threshold.value = -7
    this.compressor.knee.value = 8
    this.compressor.ratio.value = 10
    this.compressor.attack.value = 0.002
    this.compressor.release.value = 0.09
    this.compressor.connect(this.ctx.destination)

    this.master = this.ctx.createGain()
    this.master.gain.value = 0.76
    this.master.connect(this.compressor)

    this.echoDelay = this.ctx.createDelay(1)
    this.echoDelay.delayTime.value = 0.22
    this.echoFeedback = this.ctx.createGain()
    this.echoFeedback.gain.value = 0.24
    this.echoWet = this.ctx.createGain()
    this.echoWet.gain.value = 0
    this.echoDelay.connect(this.echoFeedback)
    this.echoFeedback.connect(this.echoDelay)
    this.echoDelay.connect(this.echoWet)
    this.echoWet.connect(this.compressor)
    this.master.connect(this.echoDelay)

    this.reverbConvolver = this.ctx.createConvolver()
    this.reverbConvolver.buffer = this.generateImpulse(2.1, 3.4)
    this.reverbWet = this.ctx.createGain()
    this.reverbWet.gain.value = 0
    this.reverbConvolver.connect(this.reverbWet)
    this.reverbWet.connect(this.compressor)
    this.master.connect(this.reverbConvolver)

    for (let i = 0; i < PADS; i += 1) {
      const gain = this.ctx.createGain()
      gain.gain.value = this.volumes[i]
      gain.connect(this.master)
      this.padGains[i] = gain
    }
    return this.ctx
  }

  generateImpulse(duration, decay) {
    const sr = this.ctx.sampleRate
    const len = Math.max(1, Math.round(sr * duration))
    const buffer = this.ctx.createBuffer(2, len, sr)
    for (let ch = 0; ch < 2; ch += 1) {
      const data = buffer.getChannelData(ch)
      for (let i = 0; i < len; i += 1) {
        const env = Math.pow(1 - i / len, decay)
        data[i] = (Math.random() * 2 - 1) * env
      }
    }
    return buffer
  }

  setVolume(padIdx, value) {
    const v = clamp01(value)
    this.volumes[padIdx] = v
    const gain = this.padGains[padIdx]
    if (gain && this.ctx) gain.gain.setTargetAtTime(v, this.ctx.currentTime, 0.01)
  }

  setEcho(amount) {
    const v = clamp01(amount)
    if (!this.ctx || !this.echoWet || !this.echoFeedback) return
    this.echoWet.gain.setTargetAtTime(v * 0.5, this.ctx.currentTime, 0.02)
    this.echoFeedback.gain.setTargetAtTime(0.18 + v * 0.34, this.ctx.currentTime, 0.02)
  }

  setReverb(amount) {
    const v = clamp01(amount)
    if (!this.ctx || !this.reverbWet) return
    this.reverbWet.gain.setTargetAtTime(v * 0.66, this.ctx.currentTime, 0.02)
  }

  play(padIdx, buffer, when) {
    if (!buffer || !this.ctx || !this.padGains[padIdx]) return null
    const source = this.ctx.createBufferSource()
    source.buffer = buffer
    source.connect(this.padGains[padIdx])
    source.start(typeof when === 'number' ? when : 0)
    return source
  }

  dispose() {
    if (this.ctx && this.ctx.state !== 'closed') {
      this.ctx.close().catch(() => {})
    }
    this.ctx = null
  }
}

function clamp01(value) {
  return Math.max(0, Math.min(1, Number(value) || 0))
}

function biquadFilter(data, sr, type, freq, q) {
  const w0 = (2 * Math.PI * freq) / sr
  const alpha = Math.sin(w0) / (2 * q)
  const cosW = Math.cos(w0)
  let b0
  let b1
  let b2
  let a0
  let a1
  let a2
  if (type === 'lp') {
    b0 = (1 - cosW) / 2
    b1 = 1 - cosW
    b2 = b0
    a0 = 1 + alpha
    a1 = -2 * cosW
    a2 = 1 - alpha
  } else if (type === 'hp') {
    b0 = (1 + cosW) / 2
    b1 = -(1 + cosW)
    b2 = b0
    a0 = 1 + alpha
    a1 = -2 * cosW
    a2 = 1 - alpha
  } else if (type === 'bp') {
    b0 = alpha
    b1 = 0
    b2 = -alpha
    a0 = 1 + alpha
    a1 = -2 * cosW
    a2 = 1 - alpha
  } else {
    return
  }

  b0 /= a0
  b1 /= a0
  b2 /= a0
  a1 /= a0
  a2 /= a0

  let x1 = 0
  let x2 = 0
  let y1 = 0
  let y2 = 0
  for (let i = 0; i < data.length; i += 1) {
    const x0 = data[i]
    const y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
    data[i] = y0
    x2 = x1
    x1 = x0
    y2 = y1
    y1 = y0
  }
}

function saturate(value, drive) {
  return Math.tanh(value * drive) / Math.tanh(drive)
}

function whiteNoise(length) {
  const data = new Float32Array(length)
  for (let i = 0; i < length; i += 1) data[i] = Math.random() * 2 - 1
  return data
}

function synthKick(ctx) {
  const sr = ctx.sampleRate
  const len = Math.round(sr * 0.6)
  const buffer = ctx.createBuffer(1, len, sr)
  const data = buffer.getChannelData(0)
  let phase = 0
  for (let i = 0; i < len; i += 1) {
    const t = i / sr
    const freq = 45 + 115 * Math.exp(-t * 35)
    phase += (2 * Math.PI * freq) / sr
    const body = Math.sin(phase)
    const sub = Math.sin(phase * 0.5) * 0.25 * Math.exp(-t * 8)
    const click = t < 0.004 ? (1 - t / 0.004) * 0.8 : 0
    data[i] = saturate(body * Math.exp(-t * 5.5) + sub + click, 1.6) * 0.9
  }
  biquadFilter(data, sr, 'lp', 4000, 0.7)
  return buffer
}

function synthSnare(ctx) {
  const sr = ctx.sampleRate
  const len = Math.round(sr * 0.3)
  const buffer = ctx.createBuffer(1, len, sr)
  const data = buffer.getChannelData(0)
  const noise = whiteNoise(len)
  biquadFilter(noise, sr, 'hp', 1800, 1.2)
  biquadFilter(noise, sr, 'lp', 9000, 0.8)
  let phase = 0
  for (let i = 0; i < len; i += 1) {
    const t = i / sr
    const freq = 180 + 40 * Math.exp(-t * 50)
    phase += (2 * Math.PI * freq) / sr
    const tone = Math.sin(phase) * Math.exp(-t * 35) * 0.65
    data[i] = saturate(tone + noise[i] * Math.exp(-t * 16) * 0.8, 1.3) * 0.85
  }
  return buffer
}

function synthClosedHat(ctx) {
  const sr = ctx.sampleRate
  const len = Math.round(sr * 0.07)
  const buffer = ctx.createBuffer(1, len, sr)
  const data = buffer.getChannelData(0)
  const ratios = [1, 1.4142, 1.7321, 2, 2.2361, 2.6458]
  for (let i = 0; i < len; i += 1) {
    const t = i / sr
    let v = 0
    for (const r of ratios) {
      const f = 330 * r
      v += Math.sin(2 * Math.PI * f * t) * 0.12
      v += Math.sin(2 * Math.PI * f * 3 * t) * 0.04
    }
    v += (Math.random() * 2 - 1) * 0.08
    data[i] = v * Math.exp(-t * 110) * 0.7
  }
  biquadFilter(data, sr, 'hp', 5000, 1)
  return buffer
}

function synthOpenHat(ctx) {
  const sr = ctx.sampleRate
  const len = Math.round(sr * 0.55)
  const buffer = ctx.createBuffer(1, len, sr)
  const data = buffer.getChannelData(0)
  const ratios = [1, 1.4142, 1.7321, 2, 2.2361, 2.6458]
  for (let i = 0; i < len; i += 1) {
    const t = i / sr
    let v = 0
    for (const r of ratios) {
      const f = 330 * r
      v += Math.sin(2 * Math.PI * f * t) * 0.11
      v += Math.sin(2 * Math.PI * f * 3 * t) * 0.035
    }
    v += (Math.random() * 2 - 1) * 0.06
    data[i] = v * Math.exp(-t * 5.5) * 0.55
  }
  biquadFilter(data, sr, 'hp', 4500, 0.9)
  return buffer
}

function synthClap(ctx) {
  const sr = ctx.sampleRate
  const len = Math.round(sr * 0.45)
  const buffer = ctx.createBuffer(1, len, sr)
  const data = buffer.getChannelData(0)
  const noise = whiteNoise(len)
  biquadFilter(noise, sr, 'bp', 1600, 0.8)
  for (let i = 0; i < len; i += 1) {
    const t = i / sr
    let env = 0
    for (let b = 0; b < 4; b += 1) {
      const bt = t - b * 0.007
      if (bt >= 0 && bt < 0.009) env += (1 - bt / 0.009) * 0.55
    }
    if (t > 0.03) env += Math.exp(-(t - 0.03) * 12) * 0.7
    data[i] = saturate(noise[i] * env, 1.4) * 0.8
  }
  return buffer
}

function synthRimshot(ctx) {
  const sr = ctx.sampleRate
  const len = Math.round(sr * 0.05)
  const buffer = ctx.createBuffer(1, len, sr)
  const data = buffer.getChannelData(0)
  for (let i = 0; i < len; i += 1) {
    const t = i / sr
    const tone = Math.sin(2 * Math.PI * 1720 * t)
    const sub = Math.sin(2 * Math.PI * 860 * t) * 0.65
    const click = (Math.random() * 2 - 1) * 0.15
    data[i] = (tone + sub + click) * Math.exp(-t * 90) * 0.8
  }
  biquadFilter(data, sr, 'hp', 600, 1)
  return buffer
}

function synthTomLow(ctx) {
  const sr = ctx.sampleRate
  const len = Math.round(sr * 0.55)
  const buffer = ctx.createBuffer(1, len, sr)
  const data = buffer.getChannelData(0)
  let phase = 0
  for (let i = 0; i < len; i += 1) {
    const t = i / sr
    const freq = 55 + 70 * Math.exp(-t * 14)
    phase += (2 * Math.PI * freq) / sr
    const click = t < 0.003 ? (1 - t / 0.003) * 0.5 : 0
    data[i] = saturate(Math.sin(phase) * Math.exp(-t * 5) + click, 1.3) * 0.8
  }
  biquadFilter(data, sr, 'lp', 3000, 0.7)
  return buffer
}

function synthTomHigh(ctx) {
  const sr = ctx.sampleRate
  const len = Math.round(sr * 0.4)
  const buffer = ctx.createBuffer(1, len, sr)
  const data = buffer.getChannelData(0)
  let phase = 0
  for (let i = 0; i < len; i += 1) {
    const t = i / sr
    const freq = 130 + 90 * Math.exp(-t * 18)
    phase += (2 * Math.PI * freq) / sr
    const click = t < 0.003 ? (1 - t / 0.003) * 0.4 : 0
    data[i] = saturate(Math.sin(phase) * Math.exp(-t * 7) + click, 1.3) * 0.8
  }
  biquadFilter(data, sr, 'lp', 4000, 0.7)
  return buffer
}

export const DRUM_KIT = [
  { name: 'Kick', synth: synthKick },
  { name: 'Snare', synth: synthSnare },
  { name: 'Closed Hat', synth: synthClosedHat },
  { name: 'Open Hat', synth: synthOpenHat },
  { name: 'Clap', synth: synthClap },
  { name: 'Rim', synth: synthRimshot },
  { name: 'Low Tom', synth: synthTomLow },
  { name: 'High Tom', synth: synthTomHigh },
]

export function createInitialPads() {
  return Array.from({ length: PADS }, (_, idx) => ({
    name: idx < KIT_PADS ? DRUM_KIT[idx].name : '',
    buffer: null,
    savedAudio: null,
    color: PAD_COLORS[idx],
    isPreset: idx < KIT_PADS,
  }))
}

export function installKitBuffers(pads, ctx) {
  return pads.map((pad, idx) => (
    idx < KIT_PADS
      ? { ...pad, buffer: pad.buffer || DRUM_KIT[idx].synth(ctx), isPreset: true }
      : pad
  ))
}

export function restoreSavedAudio(ctx, saved) {
  if (!saved || !Array.isArray(saved.channels) || saved.channels.length === 0) return null
  const length = Math.max(1, Number(saved.length) || 1)
  const sampleRate = Number(saved.sampleRate) || ctx.sampleRate
  const buffer = ctx.createBuffer(saved.channels.length, length, sampleRate)
  saved.channels.forEach((channel, ch) => {
    const data = base64ToFloat32(channel)
    buffer.copyToChannel(data.subarray(0, buffer.length), ch)
  })
  return buffer
}

export function createRecordingBuffer(ctx, chunks, maxSeconds = MAX_RECORD_SECONDS) {
  const maxLength = Math.max(1, Math.round(ctx.sampleRate * maxSeconds))
  const sourceLength = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const length = Math.min(sourceLength, maxLength)
  if (!length) return null

  const buffer = ctx.createBuffer(1, length, ctx.sampleRate)
  const data = buffer.getChannelData(0)
  let offset = 0
  for (const chunk of chunks) {
    if (offset >= length) break
    const next = chunk.subarray(0, Math.min(chunk.length, length - offset))
    data.set(next, offset)
    offset += next.length
  }
  normalize(data)
  return buffer
}

function normalize(data) {
  let peak = 0
  for (let i = 0; i < data.length; i += 1) peak = Math.max(peak, Math.abs(data[i]))
  if (peak < 0.01 || peak >= 0.9) return
  const gain = Math.min(0.9 / peak, 3)
  for (let i = 0; i < data.length; i += 1) data[i] *= gain
}

function serializeAudioBuffer(buffer) {
  if (!buffer) return null
  const channels = []
  for (let ch = 0; ch < buffer.numberOfChannels; ch += 1) {
    channels.push(float32ToBase64(buffer.getChannelData(ch)))
  }
  return { sampleRate: buffer.sampleRate, length: buffer.length, channels }
}

export function serializeCustomPads(pads) {
  return pads
    .map((pad, idx) => ({ pad, idx }))
    .filter(({ pad, idx }) => idx >= CUSTOM_START && (pad.buffer || pad.savedAudio))
    .map(({ pad, idx }) => ({
      idx,
      name: pad.name || `Sample ${idx - CUSTOM_START + 1}`,
      color: pad.color || PAD_COLORS[idx],
      audio: pad.buffer ? serializeAudioBuffer(pad.buffer) : pad.savedAudio,
    }))
    .filter((item) => item.audio)
}

function float32ToBase64(float32) {
  const int16 = new Int16Array(float32.length)
  for (let i = 0; i < float32.length; i += 1) {
    const v = Math.max(-1, Math.min(1, float32[i]))
    int16[i] = v < 0 ? v * 0x8000 : v * 0x7fff
  }
  const bytes = new Uint8Array(int16.buffer)
  let binary = ''
  const chunkSize = 0x8000
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize))
  }
  return btoa(binary)
}

function base64ToFloat32(base64) {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  const int16 = new Int16Array(bytes.buffer)
  const float32 = new Float32Array(int16.length)
  for (let i = 0; i < int16.length; i += 1) {
    float32[i] = Math.max(-1, int16[i] / 0x8000)
  }
  return float32
}

export function drawWaveform(canvas, audioBuffer, color) {
  if (!canvas || !audioBuffer) return
  const ctx = canvas.getContext('2d')
  const dpr = window.devicePixelRatio || 1
  const rect = canvas.getBoundingClientRect()
  const width = Math.max(1, rect.width)
  const height = Math.max(1, rect.height)
  canvas.width = Math.round(width * dpr)
  canvas.height = Math.round(height * dpr)
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, width, height)

  const data = audioBuffer.getChannelData(0)
  const step = Math.max(1, Math.ceil(data.length / width))
  const center = height / 2

  ctx.strokeStyle = 'rgba(128, 128, 140, 0.28)'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(0, center)
  ctx.lineTo(width, center)
  ctx.stroke()

  ctx.strokeStyle = color || cssVar('--accent', '#a78bfa')
  ctx.lineWidth = 1.8
  ctx.beginPath()
  for (let x = 0; x < width; x += 1) {
    let min = 1
    let max = -1
    for (let j = 0; j < step; j += 1) {
      const v = data[x * step + j] || 0
      if (v < min) min = v
      if (v > max) max = v
    }
    ctx.moveTo(x, center + min * center * 0.88)
    ctx.lineTo(x, center + max * center * 0.88)
  }
  ctx.stroke()
}

export function drawLiveWaveform(canvas, analyser) {
  if (!canvas || !analyser) return
  const ctx = canvas.getContext('2d')
  const dpr = window.devicePixelRatio || 1
  const rect = canvas.getBoundingClientRect()
  const width = Math.max(1, rect.width)
  const height = Math.max(1, rect.height)
  canvas.width = Math.round(width * dpr)
  canvas.height = Math.round(height * dpr)
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

  const data = new Uint8Array(analyser.fftSize)
  analyser.getByteTimeDomainData(data)
  ctx.clearRect(0, 0, width, height)
  ctx.strokeStyle = cssVar('--danger', '#f87171')
  ctx.lineWidth = 2
  ctx.beginPath()
  const slice = width / data.length
  for (let i = 0; i < data.length; i += 1) {
    const x = i * slice
    const y = (data[i] / 255) * height
    if (i === 0) ctx.moveTo(x, y)
    else ctx.lineTo(x, y)
  }
  ctx.stroke()
}

function cssVar(name, fallback) {
  if (typeof document === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}
