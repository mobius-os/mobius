export class SpeechGenerationOwnership {
  constructor() {
    this.activeRequestId = ''
  }

  claim(requestId) {
    if (this.activeRequestId) throw new Error('The speech reader is already speaking.')
    this.activeRequestId = requestId
  }

  cancel() {
    this.activeRequestId = ''
  }

  owns(requestId) {
    return this.activeRequestId === requestId
  }

  release(requestId) {
    if (!this.owns(requestId)) return false
    this.activeRequestId = ''
    return true
  }
}
