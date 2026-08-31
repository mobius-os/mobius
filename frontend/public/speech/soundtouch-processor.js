//#region ../core/src/AbstractSamplePipe.ts
/**
* Abstract base class for sample processing pipes.
*
* @remarks
* Manages input and output buffers for audio sample processing chains. Subclasses should implement
* specific processing logic for audio transformation or analysis. This class is not intended to be used
* directly, but as a base for concrete audio processing stages.
*
* @typeParam TInputBuffer - Concrete input buffer type (defaults to the generic `SampleBuffer` contract).
* @typeParam TOutputBuffer - Concrete output buffer type (defaults to `TInputBuffer` so input/output share the same buffer type unless a subclass opts into different types).
*/
var AbstractSamplePipe = class {
	/**
	* Input buffer for audio samples.
	*/
	_inputBuffer;
	/**
	* Output buffer for processed audio samples.
	*/
	_outputBuffer;
	/**
	* Constructs an AbstractSamplePipe.
	* @param options Constructor options.
	*
	* @remarks
	* When `createBuffers` is true, both factories are required so subclasses can
	* control exact buffer implementations without unsafe casting.
	*/
	constructor({ createBuffers = false, inputBufferFactory, outputBufferFactory } = {}) {
		if (createBuffers) {
			if (!inputBufferFactory || !outputBufferFactory) throw new Error("buffer factories are required when createBuffers is true");
			this._inputBuffer = inputBufferFactory();
			this._outputBuffer = outputBufferFactory();
		} else {
			this._inputBuffer = null;
			this._outputBuffer = null;
		}
	}
	/**
	* Gets the input buffer.
	* @returns The current input buffer instance, or null if not set.
	*/
	get inputBuffer() {
		return this._inputBuffer;
	}
	/**
	* Sets the input buffer.
	* @param inputBuffer The new input buffer instance, or null to unset.
	*/
	set inputBuffer(inputBuffer) {
		this._inputBuffer = inputBuffer;
	}
	/**
	* Gets the output buffer.
	* @returns The current output buffer instance, or null if not set.
	*/
	get outputBuffer() {
		return this._outputBuffer;
	}
	/**
	* Sets the output buffer.
	* @param outputBuffer The new output buffer instance, or null to unset.
	*/
	set outputBuffer(outputBuffer) {
		this._outputBuffer = outputBuffer;
	}
	/**
	* Clears both input and output buffers.
	*
	* @remarks
	* Resets the state of both input and output buffers, if present, by calling their `clear()` methods.
	*/
	clear() {
		this._inputBuffer?.clear();
		this._outputBuffer?.clear();
	}
};
//#endregion
//#region ../core/src/CircularSampleBuffer.ts
var SAMPLES_PER_FRAME$1 = 2;
/**
* Circular frame buffer for interleaved stereo audio samples.
*
* @remarks
* Implements a ring buffer for stereo audio, where each frame consists of two contiguous float values (left, right).
* Maintains a movable read cursor and appends at the logical end. Capacity grows automatically as needed while preserving frame order.
* Used for efficient, low-latency audio processing where buffer wraparound and dynamic resizing are required.
*/
var CircularSampleBuffer = class {
	_buffer;
	_capacityFrames;
	_readFrame;
	_frameCount;
	/**
	* @param capacityFrames Initial frame capacity before automatic growth.
	*/
	constructor(capacityFrames = 2048) {
		const normalizedCapacity = Math.max(1, Math.floor(capacityFrames));
		this._capacityFrames = normalizedCapacity;
		this._buffer = new Float32Array(normalizedCapacity * SAMPLES_PER_FRAME$1);
		this._readFrame = 0;
		this._frameCount = 0;
	}
	/**
	* Allocated capacity expressed in frames.
	* @returns The number of frames the buffer can currently hold without resizing.
	*/
	get capacityFrames() {
		return this._capacityFrames;
	}
	/**
	* Number of buffered frames currently readable.
	* @returns The number of frames available for reading.
	*/
	get frameCount() {
		return this._frameCount;
	}
	/**
	* Clears the buffer without shrinking allocated capacity.
	*
	* @remarks
	* Resets the read cursor and frame count, but does not deallocate the underlying storage.
	*/
	clear() {
		this._readFrame = 0;
		this._frameCount = 0;
	}
	/**
	* Ensures the internal storage can hold at least `minCapacityFrames`.
	*
	* @param minCapacityFrames Minimum frame capacity required.
	* @remarks
	* Grows the buffer if needed, preserving all readable frames in order.
	*/
	ensureCapacity(minCapacityFrames) {
		const normalizedMinCapacityFrames = Math.max(0, Math.floor(minCapacityFrames));
		if (normalizedMinCapacityFrames <= this._capacityFrames) return;
		const nextCapacity = Math.max(normalizedMinCapacityFrames, this._capacityFrames * 2, this._capacityFrames + 1024);
		const nextBuffer = new Float32Array(nextCapacity * SAMPLES_PER_FRAME$1);
		for (let frame = 0; frame < this._frameCount; frame += 1) {
			const sourceIndex = (this._readFrame + frame) % this._capacityFrames * SAMPLES_PER_FRAME$1;
			const destIndex = frame * SAMPLES_PER_FRAME$1;
			nextBuffer[destIndex] = this._buffer[sourceIndex];
			nextBuffer[destIndex + 1] = this._buffer[sourceIndex + 1];
		}
		this._buffer = nextBuffer;
		this._capacityFrames = nextCapacity;
		this._readFrame = 0;
	}
	/**
	* Appends source frames to the end of the ring.
	*
	* @param source Interleaved stereo source samples.
	* @param sourceFrameOffset Source offset in frames.
	* @param frameCount Number of frames to append; defaults to all complete remaining frames.
	* @remarks
	* Automatically grows the buffer if needed. Only complete frames are appended.
	*/
	pushSamples(source, sourceFrameOffset = 0, frameCount = 0) {
		const sourceStartSample = Math.max(0, Math.floor(sourceFrameOffset)) * SAMPLES_PER_FRAME$1;
		const availableFrames = Math.max(0, Math.floor((source.length - sourceStartSample) / SAMPLES_PER_FRAME$1));
		const requestedFrames = frameCount > 0 ? Math.floor(frameCount) : 0;
		const framesToWrite = requestedFrames > 0 ? Math.min(requestedFrames, availableFrames) : availableFrames;
		if (framesToWrite <= 0) return;
		this.ensureCapacity(this._frameCount + framesToWrite);
		const writeFrame = (this._readFrame + this._frameCount) % this._capacityFrames;
		for (let frame = 0; frame < framesToWrite; frame += 1) {
			const sourceIndex = sourceStartSample + frame * SAMPLES_PER_FRAME$1;
			const destIndex = (writeFrame + frame) % this._capacityFrames * SAMPLES_PER_FRAME$1;
			this._buffer[destIndex] = source[sourceIndex];
			this._buffer[destIndex + 1] = source[sourceIndex + 1];
		}
		this._frameCount += framesToWrite;
	}
	/**
	* Contract alias for `pushSamples`.
	*
	* @param source Interleaved stereo source samples.
	* @param sourceFrameOffset Source offset in frames.
	* @param frameCount Number of frames to append.
	*/
	putSamples(source, sourceFrameOffset = 0, frameCount = 0) {
		this.pushSamples(source, sourceFrameOffset, frameCount);
	}
	/**
	* Extracts frames from the ring into `target`.
	*
	* @param target Destination array for interleaved stereo samples.
	* @param sourceFrameOffset Read offset in frames.
	* @param frameCount Number of frames requested.
	* @param consume When true, consumed frames are dropped from the front.
	* @returns Number of frames copied.
	* @remarks
	* If `consume` is true, the extracted frames are removed from the buffer.
	*/
	extract(target, sourceFrameOffset = 0, frameCount = 0, consume = false) {
		const normalizedSourceFrameOffset = Math.max(0, Math.floor(sourceFrameOffset));
		const requestedFrames = frameCount > 0 ? Math.floor(frameCount) : 0;
		const framesAvailable = Math.max(0, this._frameCount - normalizedSourceFrameOffset);
		const framesToRead = requestedFrames > 0 ? Math.min(requestedFrames, framesAvailable) : framesAvailable;
		if (framesToRead <= 0) return 0;
		for (let frame = 0; frame < framesToRead; frame += 1) {
			const sourceIndex = (this._readFrame + normalizedSourceFrameOffset + frame) % this._capacityFrames * SAMPLES_PER_FRAME$1;
			const targetIndex = frame * SAMPLES_PER_FRAME$1;
			target[targetIndex] = this._buffer[sourceIndex];
			target[targetIndex + 1] = this._buffer[sourceIndex + 1];
		}
		if (consume) {
			const framesToDrop = normalizedSourceFrameOffset + framesToRead;
			this.dropFrames(framesToDrop);
		}
		return framesToRead;
	}
	/**
	* Reads a single sample value by logical sample index.
	*
	* @param sampleIndex Logical sample index relative to the readable head.
	* @returns Sample value, or `0` when the index falls outside readable data.
	* @remarks
	* Used for random access to individual samples within the readable region.
	*/
	readSample(sampleIndex) {
		const normalizedSampleIndex = Math.max(0, Math.floor(sampleIndex));
		const frameOffset = Math.floor(normalizedSampleIndex / SAMPLES_PER_FRAME$1);
		if (frameOffset >= this._frameCount) return 0;
		const channelOffset = normalizedSampleIndex % SAMPLES_PER_FRAME$1;
		const sourceIndex = (this._readFrame + frameOffset) % this._capacityFrames * SAMPLES_PER_FRAME$1 + channelOffset;
		return this._buffer[sourceIndex] ?? 0;
	}
	/**
	* Drops frames from the front of the ring.
	*
	* @param frameCount Maximum number of frames to remove.
	* @returns Number of frames removed.
	* @remarks
	* Advances the read cursor and reduces the frame count. If all frames are dropped, resets the read cursor.
	*/
	dropFrames(frameCount) {
		const framesToDrop = Math.max(0, Math.min(Math.max(0, Math.floor(frameCount)), this._frameCount));
		if (framesToDrop === 0) return 0;
		this._readFrame = (this._readFrame + framesToDrop) % this._capacityFrames;
		this._frameCount -= framesToDrop;
		if (this._frameCount === 0) this._readFrame = 0;
		return framesToDrop;
	}
	/**
	* Contract alias for `dropFrames`.
	*
	* @param frameCount Number of frames to consume.
	*/
	receive(frameCount = this._frameCount) {
		this.dropFrames(frameCount);
	}
};
//#endregion
//#region ../core/src/FifoSampleBuffer.ts
/**
* Number of bytes per sample (Float32).
*/
var BYTES_PER_SAMPLE = 4;
/**
* Number of samples per audio frame (stereo).
*/
var SAMPLES_PER_FRAME = 2;
/**
* Number of bytes per audio frame.
*/
var BYTES_PER_FRAME = BYTES_PER_SAMPLE * SAMPLES_PER_FRAME;
/**
* Default maximum number of frames for buffer allocation.
*/
var DEFAULT_MAX_FRAMES = 131072;
/**
* Resizable interleaved sample buffer for audio processing.
*
* @remarks
* Stores stereo audio samples in a contiguous Float32Array and provides methods for efficient buffer management and sample transfer.
* Uses ES2024 ArrayBuffer for zero-allocation growth. Suitable for scenarios where buffer size may need to grow dynamically during audio processing.
*/
var FifoSampleBuffer = class {
	/**
	* Backing ArrayBuffer for sample storage.
	* @remarks
	* Underlying memory for the buffer, which may be resized as needed.
	*/
	_buffer;
	/**
	* Float32Array view of the buffer.
	* @remarks
	* Provides direct access to the sample data for reading and writing.
	*/
	_vector;
	/**
	* Current read position (frame index).
	* @remarks
	* Indicates the logical start of readable data within the buffer.
	*/
	_position;
	/**
	* Number of frames currently stored.
	* @remarks
	* Represents the number of complete stereo frames available for reading.
	*/
	_frameCount;
	/**
	* Creates a new FifoSampleBuffer.
	* @param maxFrames Maximum number of frames for buffer allocation.
	*/
	constructor(maxFrames = DEFAULT_MAX_FRAMES) {
		this._buffer = new ArrayBuffer(0, { maxByteLength: maxFrames * BYTES_PER_FRAME });
		this._vector = new Float32Array(this._buffer);
		this._position = 0;
		this._frameCount = 0;
	}
	/**
	* Returns the Float32Array view of the buffer.
	* @returns The Float32Array containing the sample data.
	*/
	get vector() {
		return this._vector;
	}
	/**
	* Returns the current read position (frame index).
	* @returns The current frame index for reading.
	*/
	get position() {
		return this._position;
	}
	/**
	* Returns the start sample index for reading.
	* @returns The sample index corresponding to the start of readable data.
	*/
	get startIndex() {
		return this._position * 2;
	}
	/**
	* Returns the number of frames currently stored.
	* @returns The number of complete frames available for reading.
	*/
	get frameCount() {
		return this._frameCount;
	}
	/**
	* Returns the end sample index for reading.
	* @returns The sample index corresponding to the end of readable data.
	*/
	get endIndex() {
		return (this._position + this._frameCount) * 2;
	}
	/**
	* Clears the buffer and resets position and frame count.
	* @remarks
	* Fills the buffer with zeros and resets all internal state.
	*/
	clear() {
		this._vector.fill(0);
		this._position = 0;
		this._frameCount = 0;
	}
	/**
	* Adds empty frames to the buffer.
	* @param numFrames Number of frames to add.
	*/
	put(numFrames) {
		this._frameCount += numFrames;
	}
	/**
	* Adds samples to the buffer from a Float32Array.
	* @param samples Source samples (interleaved stereo).
	* @param position Start frame index in source.
	* @param numFrames Number of frames to copy (default: all available).
	* @remarks
	* Automatically grows the buffer if needed. Only complete frames are appended.
	*/
	putSamples(samples, position = 0, numFrames = 0) {
		const sourceOffset = position * 2;
		if (!(numFrames >= 0) || numFrames === 0) numFrames = (samples.length - sourceOffset) / 2;
		const numSamples = numFrames * 2;
		this.ensureCapacity(numFrames + this._frameCount);
		const destOffset = this.endIndex;
		this._vector.set(samples.subarray(sourceOffset, sourceOffset + numSamples), destOffset);
		this._frameCount += numFrames;
	}
	/**
	* Adds samples from another FifoSampleBuffer.
	* @param buffer Source buffer.
	* @param position Start frame index in source buffer.
	* @param numFrames Number of frames to copy (default: all available).
	*/
	putBuffer(buffer, position = 0, numFrames = 0) {
		if (!(numFrames >= 0) || numFrames === 0) numFrames = buffer.frameCount - position;
		this.putSamples(buffer.vector, buffer.position + position, numFrames);
	}
	/**
	* Advances the read position and reduces frame count.
	* @param numFrames Number of frames to receive (default: all available).
	* @remarks
	* Consumed frames are no longer available for reading.
	*/
	receive(numFrames) {
		if (numFrames === void 0 || !(numFrames >= 0) || numFrames > this._frameCount) numFrames = this._frameCount;
		this._frameCount -= numFrames;
		this._position += numFrames;
	}
	/**
	* Copies and receives samples into an output array.
	* @param output Destination Float32Array.
	* @param numFrames Number of frames to copy and receive.
	* @remarks
	* Advances the read position after copying.
	*/
	receiveSamples(output, numFrames = 0) {
		const numSamples = numFrames * 2;
		const sourceOffset = this.startIndex;
		output.set(this._vector.subarray(sourceOffset, sourceOffset + numSamples));
		this.receive(numFrames);
	}
	/**
	* Extracts samples into an output array without advancing position.
	* @param output Destination Float32Array.
	* @param position Start frame index in buffer.
	* @param numFrames Number of frames to extract.
	*/
	extract(output, position = 0, numFrames = 0) {
		const sourceOffset = this.startIndex + position * 2;
		const numSamples = numFrames * 2;
		output.set(this._vector.subarray(sourceOffset, sourceOffset + numSamples));
	}
	/**
	* Ensures the buffer has capacity for at least numFrames.
	* @param numFrames Minimum number of frames required.
	* @remarks
	* Grows the buffer if needed, preserving all readable frames in order.
	*/
	ensureCapacity(numFrames = 0) {
		const minLength = Math.floor(numFrames * SAMPLES_PER_FRAME);
		if (this._vector.length < minLength) {
			const newByteLength = minLength * BYTES_PER_SAMPLE;
			if (newByteLength <= this._buffer.maxByteLength) {
				this.rewind();
				this._buffer.resize(newByteLength);
				this._vector = new Float32Array(this._buffer);
			} else {
				const newMaxBytes = newByteLength * 2;
				const newBuffer = new ArrayBuffer(newByteLength, { maxByteLength: newMaxBytes });
				const newVector = new Float32Array(newBuffer);
				newVector.set(this._vector.subarray(this.startIndex, this.endIndex));
				this._buffer = newBuffer;
				this._vector = newVector;
				this._position = 0;
			}
		} else this.rewind();
	}
	/**
	* Ensures buffer has capacity for additional frames.
	* @param numFrames Number of additional frames required.
	*/
	ensureAdditionalCapacity(numFrames = 0) {
		this.ensureCapacity(this._frameCount + numFrames);
	}
	/**
	* Moves all unread samples to the start of the buffer.
	* @remarks
	* Compacts the buffer so that all unread samples are at the beginning, freeing space for new data.
	*/
	rewind() {
		if (this._position > 0) {
			this._vector.set(this._vector.subarray(this.startIndex, this.endIndex));
			this._position = 0;
		}
	}
};
//#endregion
//#region ../core/src/SampleBufferAdapter.ts
var FifoSampleBufferAdapter = class {
	inputBuffer;
	constructor() {
		this.inputBuffer = null;
	}
	get frameCount() {
		return this.inputBuffer?.frameCount ?? 0;
	}
	clear() {
		this.inputBuffer = null;
	}
	syncFromInputBuffer(inputBuffer) {
		this.inputBuffer = inputBuffer;
	}
	extract(target, sourceFrameOffset, frameCount) {
		const buffer = this.inputBuffer;
		if (buffer === null) return 0;
		const availableFrames = Math.max(0, buffer.frameCount - sourceFrameOffset);
		const framesToExtract = Math.max(0, Math.min(frameCount, availableFrames));
		if (framesToExtract === 0) return 0;
		buffer.extract(target, sourceFrameOffset, framesToExtract);
		return framesToExtract;
	}
	receive(frameCount) {
		this.inputBuffer?.receive(frameCount);
	}
};
var CircularSampleBufferAdapter = class {
	circularBuffer;
	scratch;
	constructor() {
		this.circularBuffer = new CircularSampleBuffer();
		this.scratch = new Float32Array(0);
	}
	get frameCount() {
		return this.circularBuffer.frameCount;
	}
	clear() {
		this.circularBuffer.clear();
	}
	syncFromInputBuffer(inputBuffer) {
		if (inputBuffer instanceof FifoSampleBuffer) {
			const frames = inputBuffer.frameCount;
			if (frames === 0) return;
			this.circularBuffer.pushSamples(inputBuffer.vector, inputBuffer.position, frames);
			inputBuffer.receive(frames);
			return;
		}
		const frames = inputBuffer.frameCount;
		if (frames === 0) return;
		const sampleCount = frames * 2;
		if (this.scratch.length < sampleCount) this.scratch = new Float32Array(sampleCount);
		inputBuffer.extract(this.scratch, 0, frames);
		this.circularBuffer.pushSamples(this.scratch, 0, frames);
		inputBuffer.receive(frames);
	}
	extract(target, sourceFrameOffset, frameCount) {
		return this.circularBuffer.extract(target, sourceFrameOffset, frameCount, false);
	}
	receive(frameCount) {
		this.circularBuffer.dropFrames(frameCount);
	}
};
/** Creates an adapter that reads directly from any `SampleBuffer` contract. */
var createFifoSampleBufferAdapter = () => new FifoSampleBufferAdapter();
/**
* Creates an adapter that stages source frames in a circular buffer for
* efficient repeated reads.
*/
var createCircularSampleBufferAdapter = () => new CircularSampleBufferAdapter();
//#endregion
//#region ../interpolation-strategy-lanczos/.dist/index.js
var LANCZOS_DEFAULT_PARAMS = {
	zeroCrossings: 4,
	normalize: false
};
function normalizeLanczosParams(params, defaults) {
	const merged = {
		...defaults,
		...params ?? {}
	};
	return {
		zeroCrossings: Math.max(2, Math.min(8, Math.round(Number(merged["zeroCrossings"] ?? defaults["zeroCrossings"] ?? 4)))),
		normalize: Boolean(merged["normalize"])
	};
}
function applyLanczosParams(state, params) {
	if (typeof state !== "object" || state === null) return;
	const record = state;
	record.params = {
		zeroCrossings: Math.max(2, Math.round(Number(params["zeroCrossings"] ?? 4))),
		normalize: Boolean(params["normalize"])
	};
}
function readFrameSample(src, srcOffset, numFrames, frameIndex, channel, state) {
	if (frameIndex < 0) return channel === 0 ? state.prevSampleL : state.prevSampleR;
	if (frameIndex >= numFrames) return src[srcOffset + 2 * (numFrames - 1) + channel];
	return src[srcOffset + 2 * frameIndex + channel];
}
function normalizedSinc(x) {
	if (x === 0) return 1;
	const value = Math.PI * x;
	return Math.sin(value) / value;
}
function lanczosWeight(distance, radius) {
	if (Math.abs(distance) >= radius) return 0;
	return normalizedSinc(distance) * normalizedSinc(distance / radius);
}
var lanczosKernel = (src, srcOffset, numFrames, position, channel, state) => {
	const kernelState = state;
	const radius = kernelState.params.zeroCrossings;
	const normalize = Boolean(kernelState.params.normalize);
	const center = Math.floor(position);
	const start = center - (radius - 1);
	const end = center + radius;
	let numerator = 0;
	let denominator = 0;
	for (let sampleIndex = start; sampleIndex <= end; sampleIndex += 1) {
		const weight = lanczosWeight(position - sampleIndex, radius);
		numerator += readFrameSample(src, srcOffset, numFrames, sampleIndex, channel, kernelState) * weight;
		denominator += weight;
	}
	if (Math.abs(denominator) < 1e-12) return readFrameSample(src, srcOffset, numFrames, Math.round(position), channel, kernelState);
	return normalize ? numerator / denominator : numerator / (denominator || 1);
};
lanczosKernel.createState = () => ({
	prevSampleL: 0,
	prevSampleR: 0,
	params: { ...LANCZOS_DEFAULT_PARAMS }
});
/** Default Lanczos strategy registration payload. */
var lanczosStrategy = {
	id: "lanczos",
	baseStrategy: "linear",
	kernel: lanczosKernel,
	defaultParams: LANCZOS_DEFAULT_PARAMS,
	normalizeParams: normalizeLanczosParams,
	applyParams: applyLanczosParams
};
//#endregion
//#region ../core/src/interpolationStrategyRegistry.ts
var strategyRegistry = /* @__PURE__ */ new Map();
var activeStrategyId = "lanczos";
function readStrategySelection(strategy) {
	if (typeof strategy === "string") return { id: strategy };
	if (strategy !== void 0) return strategy;
	return { id: activeStrategyId };
}
function readStrategyId(strategy) {
	return readStrategySelection(strategy).id;
}
function requireRegisteredStrategy(strategyId) {
	const registered = strategyRegistry.get(strategyId);
	if (registered !== void 0) return registered;
	throw new Error(`Unknown interpolation strategy id "${strategyId}". Register it before use.`);
}
function registerBuiltInInterpolationStrategy(registration) {
	const baseStrategy = registration.baseStrategy ?? "lanczos";
	strategyRegistry.set(registration.id, {
		id: registration.id,
		baseStrategy,
		builtIn: true,
		kernel: registration.kernel,
		defaultParams: { ...registration.defaultParams ?? {} },
		normalizeParams: registration.normalizeParams,
		applyParams: registration.applyParams
	});
}
function resolveKernelRegistration(registration, visited = /* @__PURE__ */ new Set()) {
	if (registration.kernel !== void 0) return registration;
	if (visited.has(registration.id)) throw new Error(`Interpolation strategy resolution cycle detected at "${registration.id}".`);
	visited.add(registration.id);
	return resolveKernelRegistration(requireRegisteredStrategy(registration.baseStrategy), visited);
}
function normalizeParams(registration, params) {
	const defaults = registration.defaultParams;
	if (registration.normalizeParams !== void 0) return registration.normalizeParams(params, defaults);
	const normalized = { ...defaults };
	if (params !== void 0) {
		for (const [key, value] of Object.entries(params)) if (value !== void 0) normalized[key] = value;
	}
	return normalized;
}
/**
* Resolves a strategy to either a built-in base id or a plugin kernel.
*
* @throws Error when the strategy id is unknown.
*/
function resolveInterpolationStrategy(strategy) {
	const registered = requireRegisteredStrategy(readStrategyId(strategy));
	if ("kernel" in registered && registered.kernel) return registered.kernel;
	return registered.baseStrategy;
}
/**
* Resolves runtime strategy state (kernel + normalized params + applier hook).
*/
function resolveInterpolationStrategyRuntime(strategy) {
	const selection = readStrategySelection(strategy);
	const registered = requireRegisteredStrategy(selection.id);
	const kernelRegistration = resolveKernelRegistration(registered);
	const kernel = kernelRegistration.kernel;
	if (kernel === void 0) throw new Error(`Interpolation strategy "${selection.id}" did not resolve to a kernel.`);
	const params = normalizeParams(registered, selection.params);
	return {
		id: registered.id,
		kernel,
		params,
		applyParams: registered.applyParams ?? kernelRegistration.applyParams
	};
}
registerBuiltInInterpolationStrategy({
	...lanczosStrategy,
	baseStrategy: "lanczos"
});
//#endregion
//#region ../core/src/RateTransposer.ts
/**
* Sample rate transposer for pitch and tempo manipulation.
*
* @remarks
* Used internally by SoundTouch for rate-based processing. Applies interpolation strategies to resample audio at different rates, supporting real-time pitch and tempo changes.
*/
var RateTransposer = class RateTransposer extends AbstractSamplePipe {
	/**
	* Current rate factor for transposition.
	*/
	_rate;
	/**
	* Source position (in frames) for the next output sample, relative to the
	* current processing block where 0 is the first frame and -1 is prevSample.
	*/
	fractionalPosition;
	/**
	* Previous left channel sample for interpolation.
	*/
	previousLeftSample;
	/**
	* Previous right channel sample for interpolation.
	*/
	previousRightSample;
	/** Scratch space used for extracted input samples. */
	inputScratch;
	/** Scratch space used for generated output samples. */
	outputScratch;
	/** Factory used when cloning or initializing adapter strategy. */
	sampleBufferAdapterFactory;
	/** Factory used to construct input/output chain buffers. */
	sampleBufferFactory;
	/** Adapter that normalizes reads from the bound input buffer. */
	inputAdapter;
	/** Selected interpolation strategy for transposition. */
	interpolationStrategy;
	/** Resolved kernel used by this transposer instance. */
	resolvedInterpolationKernel;
	/** Optional per-instance state for plugin kernels. */
	kernelState;
	/** Normalized params for the selected interpolation strategy. */
	interpolationStrategyParams;
	/** Optional params application hook from the strategy registration. */
	applyKernelParams;
	/**
	* Creates a RateTransposer instance.
	* @param options Constructor options.
	* @remarks
	* Accepts factories for buffer and adapter creation, and allows specifying the interpolation strategy.
	*/
	constructor({ createBuffers = false, sampleBufferAdapterFactory = createCircularSampleBufferAdapter, sampleBufferFactory = () => new CircularSampleBuffer(), interpolationStrategy } = {}) {
		super({
			createBuffers,
			inputBufferFactory: sampleBufferFactory,
			outputBufferFactory: sampleBufferFactory
		});
		this.fractionalPosition = -1;
		this.previousLeftSample = 0;
		this.previousRightSample = 0;
		this._rate = 1;
		this.inputScratch = new Float32Array(0);
		this.outputScratch = new Float32Array(0);
		this.sampleBufferAdapterFactory = sampleBufferAdapterFactory;
		this.sampleBufferFactory = sampleBufferFactory;
		this.inputAdapter = sampleBufferAdapterFactory();
		this.interpolationStrategy = "lanczos";
		this.resolvedInterpolationKernel = () => 0;
		this.kernelState = void 0;
		this.interpolationStrategyParams = {};
		this.applyKernelParams = void 0;
		this.setInterpolationStrategy(interpolationStrategy ?? "lanczos");
	}
	/**
	* Sets the rate factor for transposition.
	* @param rate Rate factor.
	*/
	set rate(rate) {
		this._rate = rate;
	}
	/**
	* Active interpolation strategy.
	* @returns The current interpolation strategy identifier.
	*/
	get strategy() {
		return this.interpolationStrategy;
	}
	/**
	* Active interpolation strategy params.
	* @returns The current interpolation strategy parameters.
	*/
	get strategyParams() {
		return { ...this.interpolationStrategyParams };
	}
	/**
	* Switches interpolation strategy at runtime.
	* @param strategy The new interpolation strategy to use.
	*/
	setInterpolationStrategy(strategy) {
		const resolved = resolveInterpolationStrategyRuntime(strategy);
		this.interpolationStrategy = resolved.id;
		this.resolvedInterpolationKernel = resolved.kernel;
		this.interpolationStrategyParams = { ...resolved.params };
		this.applyKernelParams = resolved.applyParams;
		if ("createState" in this.resolvedInterpolationKernel && typeof this.resolvedInterpolationKernel.createState === "function") this.kernelState = this.resolvedInterpolationKernel.createState();
		else this.kernelState = void 0;
		if (this.applyKernelParams !== void 0) this.applyKernelParams(this.kernelState, this.interpolationStrategyParams);
		this.reset();
	}
	/**
	* Applies a partial params update to the current interpolation strategy.
	* @param params Partial set of parameters to update.
	*/
	setInterpolationStrategyParams(params) {
		const nextParams = { ...this.interpolationStrategyParams };
		for (const [key, value] of Object.entries(params)) if (value !== void 0) nextParams[key] = value;
		this.interpolationStrategyParams = nextParams;
		if (this.applyKernelParams !== void 0) this.applyKernelParams(this.kernelState, this.interpolationStrategyParams);
	}
	/**
	* Resets internal state for interpolation.
	* @remarks
	* Clears previous sample values and resets the fractional position for output generation.
	*/
	reset() {
		this.fractionalPosition = -1;
		this.previousLeftSample = 0;
		this.previousRightSample = 0;
	}
	/**
	* Clears buffers and resets internal state.
	* @remarks
	* Calls clear on all internal buffers and resets interpolation state.
	*/
	clear() {
		super.clear();
		this.inputAdapter.clear();
		this.reset();
	}
	/**
	* Creates a clone of this RateTransposer with the same rate.
	* @returns Cloned RateTransposer instance.
	*/
	clone() {
		const result = new RateTransposer({
			createBuffers: false,
			sampleBufferAdapterFactory: this.sampleBufferAdapterFactory,
			sampleBufferFactory: this.sampleBufferFactory,
			interpolationStrategy: {
				id: this.interpolationStrategy,
				params: this.interpolationStrategyParams
			}
		});
		result.rate = this._rate;
		return result;
	}
	/**
	* Processes input buffer and writes transposed samples to output buffer.
	* @remarks
	* Reads frames from the input buffer, applies rate transposition, and writes to the output buffer.
	*/
	process() {
		if (this._inputBuffer === null || this._outputBuffer === null) return;
		this.inputAdapter.syncFromInputBuffer(this._inputBuffer);
		const numFrames = this.inputAdapter.frameCount;
		if (numFrames === 0) return;
		const numFramesOutput = this.transpose(numFrames);
		this.inputAdapter.receive(numFrames);
		if (numFramesOutput > 0) this._outputBuffer.putSamples(this.outputScratch, 0, numFramesOutput);
	}
	/**
	* Ensures temporary scratch arrays are large enough for the current frame request and estimated output size.
	*
	* @param numInputFrames Number of input frames that will be processed.
	* @remarks
	* Allocates or resizes scratch arrays as needed for efficient processing.
	*/
	ensureScratchCapacity(numInputFrames) {
		const inputSamples = numInputFrames * 2;
		if (this.inputScratch.length < inputSamples) this.inputScratch = new Float32Array(inputSamples);
		const estimatedOutputFrames = Math.ceil(numInputFrames / this._rate) + 2;
		const outputSamples = Math.max(0, estimatedOutputFrames) * 2;
		if (this.outputScratch.length < outputSamples) this.outputScratch = new Float32Array(outputSamples);
	}
	/**
	* Transposes input samples by the current rate.
	* @param numFrames Number of input frames to transpose.
	* @returns Number of output frames written.
	* @remarks
	* Applies the selected interpolation kernel to generate output samples at the new rate.
	*/
	transpose(numFrames = 0) {
		if (this._inputBuffer !== null) {
			this.inputAdapter.syncFromInputBuffer(this._inputBuffer);
			if (numFrames === 0) numFrames = this.inputAdapter.frameCount;
		}
		if (numFrames === 0) return 0;
		this.ensureScratchCapacity(numFrames);
		const src = this.inputScratch;
		const extractedFrames = this.inputAdapter.extract(src, 0, numFrames);
		if (extractedFrames === 0) return 0;
		numFrames = extractedFrames;
		return this.transposePluginKernel(numFrames);
	}
	/**
	* Handles transposition using a plugin kernel.
	* @remarks
	* Invokes the selected interpolation kernel for each output sample.
	*/
	transposePluginKernel(numFrames) {
		const src = this.inputScratch;
		const dest = this.outputScratch;
		const srcOffset = 0;
		const destOffset = 0;
		const kernel = this.resolvedInterpolationKernel;
		const state = this.kernelState;
		const stateRecord = this.getKernelStateRecord(state);
		if (stateRecord !== void 0) {
			stateRecord.prevSampleL = this.previousLeftSample;
			stateRecord.prevSampleR = this.previousRightSample;
		}
		let i = 0;
		let position = this.fractionalPosition;
		const maxPosition = numFrames - 1;
		while (position <= maxPosition) {
			dest[destOffset + 2 * i] = kernel(src, srcOffset, numFrames, position, 0, state);
			dest[destOffset + 2 * i + 1] = kernel(src, srcOffset, numFrames, position, 1, state);
			i = i + 1;
			position += this._rate;
		}
		this.fractionalPosition = position - numFrames;
		this.previousLeftSample = src[srcOffset + 2 * numFrames - 2];
		this.previousRightSample = src[srcOffset + 2 * numFrames - 1];
		if (stateRecord !== void 0) {
			stateRecord.prevSampleL = this.previousLeftSample;
			stateRecord.prevSampleR = this.previousRightSample;
		}
		return i;
	}
	/**
	* Returns the kernel state record if available.
	* @param state The kernel state object.
	* @returns The state record with previous sample values, or undefined if not present.
	*/
	getKernelStateRecord(state) {
		if (typeof state !== "object" || state === null) return;
		const record = state;
		const prevSampleL = record["prevSampleL"];
		const prevSampleR = record["prevSampleR"];
		if (typeof prevSampleL === "number" && typeof prevSampleR === "number") return record;
	}
};
//#endregion
//#region ../core/src/Stretch.ts
/**
* Read adapter optimized for FIFO-backed buffers with a generic fallback path.
*/
var FifoStretchBufferAdapter = class {
	buffer;
	fallbackBuffer;
	fallbackScratch;
	constructor() {
		this.buffer = null;
		this.fallbackBuffer = new FifoSampleBuffer();
		this.fallbackScratch = new Float32Array(0);
	}
	/**
	* @param buffer Source buffer to expose through FIFO-style reads.
	*/
	setBuffer(buffer) {
		if (buffer instanceof FifoSampleBuffer) {
			this.buffer = buffer;
			return;
		}
		const frameCount = buffer.frameCount;
		if (frameCount > 0) {
			const sampleCount = frameCount * 2;
			if (this.fallbackScratch.length < sampleCount) this.fallbackScratch = new Float32Array(sampleCount);
			buffer.extract(this.fallbackScratch, 0, frameCount);
			this.fallbackBuffer.clear();
			this.fallbackBuffer.putSamples(this.fallbackScratch, 0, frameCount);
			buffer.receive(frameCount);
		} else this.fallbackBuffer.clear();
		this.buffer = this.fallbackBuffer;
	}
	/**
	* Returns the currently bound FIFO buffer.
	* @throws Error when `setBuffer` has not been called yet.
	*/
	getBoundBuffer() {
		if (this.buffer === null) throw new Error("buffer is not set");
		return this.buffer;
	}
	get frameCount() {
		return this.getBoundBuffer().frameCount;
	}
	get startIndex() {
		return this.getBoundBuffer().startIndex;
	}
	readSample(sampleIndex) {
		const boundBuffer = this.getBoundBuffer();
		const start = boundBuffer.startIndex;
		const end = start + boundBuffer.frameCount * 2;
		if (sampleIndex < start || sampleIndex >= end) return 0;
		return boundBuffer.vector[sampleIndex];
	}
	readSubarray(start, end) {
		return this.getBoundBuffer().vector.subarray(start, end);
	}
	receive(numFrames) {
		this.getBoundBuffer().receive(numFrames);
	}
	receiveSamples(output, numFrames) {
		this.getBoundBuffer().receiveSamples(output, numFrames);
	}
};
var GenericStretchWriteBufferAdapter = class {
	buffer;
	constructor() {
		this.buffer = null;
	}
	setOutputBuffer(buffer) {
		this.buffer = buffer;
	}
	/**
	* Returns the currently bound output buffer.
	* @throws Error when `setOutputBuffer` has not been called.
	*/
	getBoundBuffer() {
		if (this.buffer === null) throw new Error("output buffer is not set");
		return this.buffer;
	}
	appendSamples(samples, numFrames) {
		this.getBoundBuffer().putSamples(samples, 0, numFrames);
	}
	putFrom(source, position, numFrames) {
		const sourceStart = source.startIndex + position * 2;
		const sourceEnd = sourceStart + numFrames * 2;
		const chunk = source.readSubarray(sourceStart, sourceEnd);
		this.getBoundBuffer().putSamples(chunk, 0, numFrames);
	}
};
var CircularStretchInputBufferAdapter = class {
	circularBuffer;
	rangeScratch;
	constructor() {
		this.circularBuffer = new CircularSampleBuffer();
		this.rangeScratch = new Float32Array(0);
	}
	/**
	* Binds a source buffer and stages its readable frames into the internal
	* circular storage.
	*
	* @param buffer Source buffer to import.
	*/
	setBuffer(buffer) {
		if (buffer instanceof FifoSampleBuffer) {
			const frames = buffer.frameCount;
			if (frames > 0) {
				this.circularBuffer.pushSamples(buffer.vector, buffer.position, frames);
				buffer.receive(frames);
			}
			return;
		}
		const frames = buffer.frameCount;
		if (frames > 0) {
			const sampleCount = frames * 2;
			if (this.rangeScratch.length < sampleCount) this.rangeScratch = new Float32Array(sampleCount);
			buffer.extract(this.rangeScratch, 0, frames);
			this.circularBuffer.pushSamples(this.rangeScratch, 0, frames);
			buffer.receive(frames);
		}
	}
	get frameCount() {
		return this.circularBuffer.frameCount;
	}
	get startIndex() {
		return 0;
	}
	readSample(sampleIndex) {
		return this.circularBuffer.readSample(sampleIndex);
	}
	/**
	* Returns a contiguous range from circular storage, padding trailing values
	* with zeros when the requested range extends past available data.
	*/
	readSubarray(start, end) {
		const normalizedStart = Math.max(0, Math.floor(start));
		const requestedSamples = Math.max(normalizedStart, Math.floor(end)) - normalizedStart;
		const requestedFrames = Math.floor(requestedSamples / 2);
		if (requestedFrames <= 0) return this.rangeScratch.subarray(0, 0);
		const needed = requestedFrames * 2;
		if (this.rangeScratch.length < needed) this.rangeScratch = new Float32Array(needed);
		const sourceFrameOffset = Math.floor(normalizedStart / 2);
		const readSamples = this.circularBuffer.extract(this.rangeScratch, sourceFrameOffset, requestedFrames, false) * 2;
		if (readSamples < needed) this.rangeScratch.fill(0, readSamples, needed);
		return this.rangeScratch.subarray(0, needed);
	}
	receive(numFrames) {
		this.circularBuffer.dropFrames(numFrames);
	}
	receiveSamples(output, numFrames) {
		this.circularBuffer.extract(output, 0, numFrames, true);
	}
};
/**
* Creates a stretch input adapter that reads from FIFO-compatible buffers.
*/
var createFifoStretchInputBufferAdapter = () => new FifoStretchBufferAdapter();
/**
* Creates a stretch input adapter backed by `CircularSampleBuffer`.
*/
var createCircularStretchInputBufferAdapter = () => new CircularStretchInputBufferAdapter();
var DEFAULT_SEQUENCE_MS = 0;
var DEFAULT_SEEKWINDOW_MS = 0;
var DEFAULT_OVERLAP_MS = 8;
var AUTOSEQ_TEMPO_LOW = .25;
var AUTOSEQ_TEMPO_TOP = 4;
var AUTOSEQ_AT_MIN = 125;
var AUTOSEQ_AT_MAX = 50;
var AUTOSEQ_K = (AUTOSEQ_AT_MAX - AUTOSEQ_AT_MIN) / (AUTOSEQ_TEMPO_TOP - AUTOSEQ_TEMPO_LOW);
var AUTOSEQ_C = AUTOSEQ_AT_MIN - AUTOSEQ_K * AUTOSEQ_TEMPO_LOW;
var AUTOSEEK_AT_MIN = 25;
var AUTOSEEK_AT_MAX = 15;
var AUTOSEEK_K = (AUTOSEEK_AT_MAX - AUTOSEEK_AT_MIN) / (AUTOSEQ_TEMPO_TOP - AUTOSEQ_TEMPO_LOW);
var AUTOSEEK_C = AUTOSEEK_AT_MIN - AUTOSEEK_K * AUTOSEQ_TEMPO_LOW;
var NORMALIZED_CORRELATION_EPSILON = 1e-12;
var QUICK_SEEK_FALLBACK_THRESHOLD = 256;
var QUICK_SEEK_MIN_VALID_CANDIDATES = 8;
/**
* Time-stretch processor for tempo adjustment without affecting pitch.
* Used internally by SoundTouch for time-stretching audio.
*/
var Stretch = class Stretch extends AbstractSamplePipe {
	inputBufferAdapterFactory;
	sampleBufferFactory;
	inputBufferAdapter;
	outputBufferAdapter;
	overlapScratch;
	_quickSeek;
	midBufferDirty;
	midBuffer;
	refMidBuffer;
	refMidBufferEnergy;
	overlapLength;
	autoSeqSetting;
	autoSeekSetting;
	_tempo;
	sampleRate;
	_overlapMs;
	sequenceMs;
	seekWindowMs;
	seekWindowLength;
	seekLength;
	nominalSkip;
	skipFract;
	sampleReq;
	/**
	* Creates a Stretch instance.
	* @param options Constructor options.
	*/
	constructor({ sampleRate = 44100, createBuffers = false, inputBufferAdapterFactory = createFifoStretchInputBufferAdapter, sampleBufferFactory = () => new FifoSampleBuffer() } = {}) {
		super({
			createBuffers,
			inputBufferFactory: sampleBufferFactory,
			outputBufferFactory: sampleBufferFactory
		});
		this.inputBufferAdapterFactory = inputBufferAdapterFactory;
		this.sampleBufferFactory = sampleBufferFactory;
		this.inputBufferAdapter = inputBufferAdapterFactory();
		this.outputBufferAdapter = new GenericStretchWriteBufferAdapter();
		this.overlapScratch = new Float32Array(0);
		this._quickSeek = true;
		this.midBufferDirty = true;
		this.midBuffer = null;
		this.refMidBufferEnergy = 0;
		this.overlapLength = 0;
		this.autoSeqSetting = true;
		this.autoSeekSetting = true;
		this._tempo = 1;
		this.setParameters(sampleRate, DEFAULT_SEQUENCE_MS, DEFAULT_SEEKWINDOW_MS, DEFAULT_OVERLAP_MS);
	}
	clear() {
		super.clear();
		this.clearMidBuffer();
	}
	clearMidBuffer() {
		this.midBufferDirty = true;
		if (this.midBuffer) this.midBuffer.fill(0);
		if (this.refMidBuffer) this.refMidBuffer.fill(0);
		this.skipFract = 0;
	}
	setParameters(sampleRate, sequenceMs, seekWindowMs, overlapMs) {
		if (sampleRate > 0) this.sampleRate = sampleRate;
		if (overlapMs > 0) this._overlapMs = overlapMs;
		if (sequenceMs > 0) {
			this.sequenceMs = sequenceMs;
			this.autoSeqSetting = false;
		} else this.autoSeqSetting = true;
		if (seekWindowMs > 0) {
			this.seekWindowMs = seekWindowMs;
			this.autoSeekSetting = false;
		} else this.autoSeekSetting = true;
		this.calculateSequenceParameters();
		this.calculateOverlapLength(this._overlapMs);
		this.updateTempoDerivedState();
	}
	set tempo(newTempo) {
		this._tempo = newTempo;
		this.updateTempoDerivedState();
	}
	get tempo() {
		return this._tempo;
	}
	get inputChunkSize() {
		return this.sampleReq;
	}
	get outputChunkSize() {
		return this.overlapLength + Math.max(0, this.seekWindowLength - 2 * this.overlapLength);
	}
	calculateOverlapLength(overlapInMsec = 0) {
		let newOvl = this.sampleRate * overlapInMsec / 1e3;
		newOvl = newOvl < 16 ? 16 : newOvl;
		newOvl -= newOvl % 8;
		if (newOvl === this.overlapLength && this.midBuffer !== null) return;
		this.overlapLength = newOvl;
		const needed = this.overlapLength * 2;
		if (!this.refMidBuffer || this.refMidBuffer.length < needed) this.refMidBuffer = new Float32Array(needed);
		if (!this.midBuffer || this.midBuffer.length < needed) this.midBuffer = new Float32Array(needed);
	}
	checkLimits(x, mi, ma) {
		return x < mi ? mi : x > ma ? ma : x;
	}
	calculateSequenceParameters() {
		if (this.autoSeqSetting) {
			let seq = AUTOSEQ_C + AUTOSEQ_K * this._tempo;
			seq = this.checkLimits(seq, AUTOSEQ_AT_MAX, AUTOSEQ_AT_MIN);
			this.sequenceMs = Math.floor(seq + .5);
		}
		if (this.autoSeekSetting) {
			let seek = AUTOSEEK_C + AUTOSEEK_K * this._tempo;
			seek = this.checkLimits(seek, AUTOSEEK_AT_MAX, AUTOSEEK_AT_MIN);
			this.seekWindowMs = Math.floor(seek + .5);
		}
		this.seekWindowLength = Math.floor(this.sampleRate * this.sequenceMs / 1e3);
		this.seekLength = Math.floor(this.sampleRate * this.seekWindowMs / 1e3);
		this.normalizeWindowInvariants();
	}
	normalizeWindowInvariants() {
		this.seekLength = Math.max(1, this.seekLength);
		this.seekWindowLength = Math.max(this.seekWindowLength, this.overlapLength);
	}
	updateTempoDerivedState() {
		this.calculateSequenceParameters();
		this.nominalSkip = this._tempo * (this.seekWindowLength - this.overlapLength);
		this.skipFract = 0;
		const intskip = Math.floor(this.nominalSkip + .5);
		this.sampleReq = Math.max(intskip + this.overlapLength, this.seekWindowLength) + this.seekLength;
	}
	/**
	* Whether the fast multi-pass seek algorithm is active.
	* @returns `true` if quick seek is enabled (default); `false` for exhaustive search.
	*/
	get quickSeek() {
		return this._quickSeek;
	}
	set quickSeek(enable) {
		this._quickSeek = enable;
	}
	/**
	* Current overlap crossfade length in milliseconds.
	* @returns The overlap period used at the current sample rate.
	*/
	get overlapMs() {
		return this._overlapMs;
	}
	/**
	* Sets the overlap crossfade length and recalculates derived parameters.
	* @param ms Overlap period in milliseconds (must be > 0).
	*/
	set overlapMs(ms) {
		if (ms > 0) {
			this._overlapMs = ms;
			this.calculateOverlapLength(this._overlapMs);
			this.calculateSequenceParameters();
			this.updateTempoDerivedState();
		}
	}
	/**
	* Applies a partial set of WSOLA timing parameters.
	*
	* @remarks
	* Only the provided fields are updated; omitted fields remain unchanged.
	* Pass `sequenceMs: 0` or `seekWindowMs: 0` to switch that dimension back to auto-calculation.
	*
	* @param params Partial set of WSOLA timing parameters to apply.
	*
	* @example
	* stretch.setStretchParameters({ overlapMs: 12, quickSeek: false });
	*/
	setStretchParameters(params) {
		if (params.quickSeek !== void 0) this._quickSeek = params.quickSeek;
		let needsRecalc = false;
		if (params.sequenceMs !== void 0) {
			if (params.sequenceMs > 0) {
				this.sequenceMs = params.sequenceMs;
				this.autoSeqSetting = false;
			} else this.autoSeqSetting = true;
			needsRecalc = true;
		}
		if (params.seekWindowMs !== void 0) {
			if (params.seekWindowMs > 0) {
				this.seekWindowMs = params.seekWindowMs;
				this.autoSeekSetting = false;
			} else this.autoSeekSetting = true;
			needsRecalc = true;
		}
		if (params.overlapMs !== void 0 && params.overlapMs > 0) {
			this._overlapMs = params.overlapMs;
			this.calculateOverlapLength(this._overlapMs);
			needsRecalc = true;
		}
		if (needsRecalc) {
			this.calculateSequenceParameters();
			this.updateTempoDerivedState();
		}
	}
	clone() {
		const result = new Stretch({
			createBuffers: false,
			inputBufferAdapterFactory: this.inputBufferAdapterFactory,
			sampleBufferFactory: this.sampleBufferFactory
		});
		result.tempo = this._tempo;
		result.setParameters(this.sampleRate, this.sequenceMs, this.seekWindowMs, this._overlapMs);
		return result;
	}
	seekBestOverlapPosition(inputBuffer) {
		const resolvedInputBuffer = inputBuffer ?? this.getInputBufferAdapter();
		if (!this._quickSeek || this.seekLength <= QUICK_SEEK_FALLBACK_THRESHOLD) return this.seekBestOverlapPositionStereo(resolvedInputBuffer);
		return this.seekBestOverlapPositionStereoQuick(resolvedInputBuffer);
	}
	seekBestOverlapPositionStereo(inputBuffer) {
		let bestOffset;
		let bestCorrelation;
		let correlation;
		this.preCalculateCorrelationReferenceStereo();
		bestOffset = 0;
		bestCorrelation = -Infinity;
		for (let i = 0; i < this.seekLength; i++) {
			correlation = this.calculateCrossCorrelationStereo(2 * i, this.refMidBuffer, inputBuffer);
			if (correlation > bestCorrelation) {
				bestCorrelation = correlation;
				bestOffset = i;
			}
		}
		return bestOffset;
	}
	seekBestOverlapPositionStereoQuick(inputBuffer) {
		let bestOffset;
		let bestCorrelation;
		let correlation;
		let correlationOffset;
		let tempOffset;
		let evaluatedCandidates;
		this.preCalculateCorrelationReferenceStereo();
		bestCorrelation = this.calculateCrossCorrelationStereo(0, this.refMidBuffer, inputBuffer);
		evaluatedCandidates = 1;
		bestOffset = 0;
		correlationOffset = 0;
		for (let scanCount = 0; scanCount < 4; scanCount++) {
			let previousTempOffset = Number.MIN_SAFE_INTEGER;
			const scanOffsets = this.getQuickScanOffsets(scanCount);
			for (const scanOffset of scanOffsets) {
				tempOffset = correlationOffset + scanOffset;
				if (tempOffset === previousTempOffset) continue;
				previousTempOffset = tempOffset;
				if (tempOffset < 0) continue;
				if (tempOffset >= this.seekLength) continue;
				correlation = this.calculateCrossCorrelationStereo(2 * tempOffset, this.refMidBuffer, inputBuffer);
				evaluatedCandidates++;
				if (correlation > bestCorrelation) {
					bestCorrelation = correlation;
					bestOffset = tempOffset;
				}
			}
			correlationOffset = bestOffset;
		}
		if (evaluatedCandidates < QUICK_SEEK_MIN_VALID_CANDIDATES) return this.seekBestOverlapPositionStereo(inputBuffer);
		return bestOffset;
	}
	getQuickScanOffsets(stage) {
		const maxOffset = Math.max(1, this.seekLength - 1);
		if (stage === 0) return this.generateFractionalScanOffsets(maxOffset, 2, 1, 14, 24);
		if (stage === 1) return this.generateSymmetricScanOffsets(maxOffset, .2);
		if (stage === 2) return this.generateSymmetricScanOffsets(maxOffset, .06);
		return this.generateSymmetricScanOffsets(maxOffset, .015);
	}
	generateFractionalScanOffsets(maxOffset, startNumerator, stepNumerator, denominator, steps) {
		const offsets = [];
		const seen = /* @__PURE__ */ new Set();
		const safeDenominator = Math.max(1, denominator);
		const safeSteps = Math.max(1, steps);
		for (let i = 0; i < safeSteps; i++) {
			const numerator = startNumerator + i * stepNumerator;
			const value = Math.round(maxOffset * numerator / safeDenominator);
			if (value <= 0 || value >= this.seekLength || seen.has(value)) continue;
			seen.add(value);
			offsets.push(value);
		}
		return offsets;
	}
	generateSymmetricScanOffsets(maxOffset, spanRatio) {
		const span = Math.max(1, Math.round(maxOffset * spanRatio));
		const scales = [
			1,
			.75,
			.5,
			.25
		];
		const negative = [];
		const positive = [];
		const seen = /* @__PURE__ */ new Set();
		for (const scale of scales) {
			const magnitude = Math.max(1, Math.round(span * scale));
			const neg = -magnitude;
			const pos = magnitude;
			if (!seen.has(neg)) {
				seen.add(neg);
				negative.push(neg);
			}
			if (!seen.has(pos)) {
				seen.add(pos);
				positive.push(pos);
			}
		}
		return negative.concat(positive);
	}
	preCalculateCorrelationReferenceStereo() {
		let energy = 0;
		for (let i = 0; i < this.overlapLength; i++) {
			const temp = i * (this.overlapLength - i);
			const ctx = i * 2;
			const left = this.midBuffer[ctx] * temp;
			const right = this.midBuffer[ctx + 1] * temp;
			this.refMidBuffer[ctx] = left;
			this.refMidBuffer[ctx + 1] = right;
			energy += left * left + right * right;
		}
		this.refMidBufferEnergy = energy;
	}
	calculateCrossCorrelationStereo(mixingPos, compare, inputBuffer) {
		mixingPos += inputBuffer.startIndex;
		let dot = 0;
		let sourceEnergy = 0;
		const calcLength = 2 * this.overlapLength;
		const source = inputBuffer.readSubarray(mixingPos, mixingPos + calcLength);
		for (let i = 0; i < calcLength; i += 2) {
			const sourceLeft = i < source.length ? source[i] : 0;
			const sourceRight = i + 1 < source.length ? source[i + 1] : 0;
			const compareLeft = compare[i];
			const compareRight = compare[i + 1];
			dot += sourceLeft * compareLeft + sourceRight * compareRight;
			sourceEnergy += sourceLeft * sourceLeft + sourceRight * sourceRight;
		}
		if (sourceEnergy <= NORMALIZED_CORRELATION_EPSILON || this.refMidBufferEnergy <= NORMALIZED_CORRELATION_EPSILON) return -1;
		return dot / Math.sqrt(sourceEnergy * this.refMidBufferEnergy);
	}
	overlapStereo(inputPosition, inputBuffer, outputBuffer) {
		inputPosition += inputBuffer.startIndex;
		const overlapSamples = this.overlapLength * 2;
		if (this.overlapScratch.length < overlapSamples) this.overlapScratch = new Float32Array(overlapSamples);
		const output = this.overlapScratch;
		const input = inputBuffer.readSubarray(inputPosition, inputPosition + overlapSamples);
		const frameScale = 1 / this.overlapLength;
		for (let i = 0; i < this.overlapLength; i++) {
			const tempFrame = (this.overlapLength - i) * frameScale;
			const fi = i * frameScale;
			const ctx = 2 * i;
			const inputLeft = ctx < input.length ? input[ctx] : 0;
			const inputRight = ctx + 1 < input.length ? input[ctx + 1] : 0;
			output[ctx] = inputLeft * fi + this.midBuffer[ctx] * tempFrame;
			output[ctx + 1] = inputRight * fi + this.midBuffer[ctx + 1] * tempFrame;
		}
		outputBuffer.appendSamples(output, this.overlapLength);
	}
	process() {
		const inputBuffer = this.getInputBufferAdapter();
		const outputBuffer = this.getOutputBufferAdapter();
		if (!this.bootstrapMidBuffer(inputBuffer)) return;
		while (inputBuffer.frameCount >= this.sampleReq) this.processOneWindow(inputBuffer, outputBuffer);
	}
	bootstrapMidBuffer(inputBuffer) {
		if (!this.midBufferDirty) return true;
		if (inputBuffer.frameCount < this.overlapLength) return false;
		const needed = this.overlapLength * 2;
		if (!this.midBuffer || this.midBuffer.length < needed) this.midBuffer = new Float32Array(needed);
		inputBuffer.receiveSamples(this.midBuffer, this.overlapLength);
		this.midBufferDirty = false;
		return true;
	}
	processOneWindow(inputBuffer, outputBuffer) {
		const offset = this.seekBestOverlapPosition(inputBuffer);
		this.overlapStereo(2 * Math.floor(offset), inputBuffer, outputBuffer);
		const middleFrames = this.seekWindowLength - 2 * this.overlapLength;
		if (middleFrames > 0) outputBuffer.putFrom(inputBuffer, offset + this.overlapLength, middleFrames);
		this.captureOverlapHistory(offset, inputBuffer);
		this.advanceInputByNominalSkip(inputBuffer);
	}
	captureOverlapHistory(offset, inputBuffer) {
		const start = inputBuffer.startIndex + 2 * (offset + this.seekWindowLength - this.overlapLength);
		this.midBuffer.set(inputBuffer.readSubarray(start, start + 2 * this.overlapLength));
	}
	advanceInputByNominalSkip(inputBuffer) {
		this.skipFract += this.nominalSkip;
		const overlapSkip = Math.floor(this.skipFract);
		this.skipFract -= overlapSkip;
		inputBuffer.receive(overlapSkip);
	}
	getInputBufferAdapter() {
		if (this._inputBuffer === null) throw new Error("inputBuffer is not set");
		this.inputBufferAdapter.setBuffer(this._inputBuffer);
		return this.inputBufferAdapter;
	}
	getOutputBufferAdapter() {
		if (this._outputBuffer === null) throw new Error("outputBuffer is not set");
		this.outputBufferAdapter.setOutputBuffer(this._outputBuffer);
		return this.outputBufferAdapter;
	}
};
//#endregion
//#region ../core/src/testFloatEqual.ts
/**
* Tests whether two floating-point numbers differ beyond a fixed epsilon.
*
* @param a First number to compare.
* @param b Second number to compare.
* @returns True when the numbers differ by more than epsilon, false otherwise.
* @remarks
* Uses a fixed epsilon threshold to determine significant difference.
*/
function isFloatDifferent(a, b) {
	return (a > b ? a - b : b - a) > 1e-10;
}
//#endregion
//#region ../core/src/SoundTouch.ts
/**
* Main processing engine for pitch shifting and time-stretching.
*
* @remarks
* Chains a `RateTransposer` and `Stretch` stage to deliver real-time pitch manipulation
* without affecting playback tempo. Set `pitch`, `pitchOctaves`, or `pitchSemitones` to
* control the output. The internal `_rate` and `_tempo` pipeline values are derived
* automatically from `virtualPitch`.
*/
var SoundTouch = class SoundTouch {
	transposer;
	stretch;
	_sampleRate;
	_sampleBufferType;
	_sampleBufferFactory;
	_interpolationStrategy;
	_inputBuffer;
	_intermediateBuffer;
	_outputBuffer;
	_rate;
	_tempo;
	/** Current pitch multiplier. Updated by the `pitch`, `pitchOctaves`, and `pitchSemitones` setters. */
	virtualPitch;
	/**
	* Creates a new SoundTouch processor instance.
	* @param options Construction options for sample rate, buffer strategy, and factories.
	*/
	constructor(options = {}) {
		this._sampleBufferType = options.sampleBufferType ?? "circular";
		this._sampleBufferFactory = options.sampleBufferFactory ?? (this._sampleBufferType === "fifo" ? () => new FifoSampleBuffer() : () => new CircularSampleBuffer());
		this.transposer = new RateTransposer({
			createBuffers: false,
			sampleBufferAdapterFactory: this._sampleBufferType === "circular" ? createCircularSampleBufferAdapter : createFifoSampleBufferAdapter,
			sampleBufferFactory: this._sampleBufferFactory,
			interpolationStrategy: options.interpolationStrategy
		});
		this._interpolationStrategy = this.transposer.strategy;
		this._sampleRate = options.sampleRate ?? 44100;
		if (options.stretchFactory) this.stretch = options.stretchFactory(this._sampleRate, {
			sampleBufferFactory: this._sampleBufferFactory,
			sampleBufferType: this._sampleBufferType
		});
		else this.stretch = new Stretch({
			sampleRate: this._sampleRate,
			createBuffers: false,
			inputBufferAdapterFactory: this._sampleBufferType === "circular" ? createCircularStretchInputBufferAdapter : createFifoStretchInputBufferAdapter,
			sampleBufferFactory: this._sampleBufferFactory
		});
		this.stretch.setParameters(this._sampleRate, 0, 0, 0);
		this._inputBuffer = this._sampleBufferFactory();
		this._intermediateBuffer = this._sampleBufferFactory();
		this._outputBuffer = this._sampleBufferFactory();
		this._rate = 0;
		this._tempo = 0;
		this.virtualPitch = 1;
		this.calculateEffectiveRateAndTempo();
	}
	/**
	* Clears both processing stages and their internal buffers.
	* @remarks
	* Resets the state of the transposer and stretch stages, including all internal buffers.
	*/
	clear() {
		this.transposer.clear();
		this.stretch.clear();
	}
	/**
	* Creates an independent copy with equivalent runtime configuration.
	*/
	clone() {
		const result = new SoundTouch({
			sampleRate: this._sampleRate,
			sampleBufferType: this._sampleBufferType,
			sampleBufferFactory: this._sampleBufferFactory,
			interpolationStrategy: {
				id: this._interpolationStrategy,
				params: this.transposer.strategyParams
			}
		});
		result.pitch = this.virtualPitch;
		return result;
	}
	/**
	* Active interpolation strategy id used by the transposer stage.
	* @returns The current interpolation strategy identifier.
	*/
	get interpolationStrategy() {
		return this._interpolationStrategy;
	}
	/**
	* Active interpolation strategy params used by the transposer stage.
	* @returns The current interpolation strategy parameters.
	*/
	get interpolationStrategyParams() {
		return this.transposer.strategyParams;
	}
	/**
	* Switches interpolation strategy at runtime.
	* @param strategy The new interpolation strategy to use.
	*/
	setInterpolationStrategy(strategy) {
		this.transposer.setInterpolationStrategy(strategy);
		this._interpolationStrategy = this.transposer.strategy;
	}
	/**
	* Applies a partial runtime params update to the current strategy.
	* @param params Partial set of parameters to update.
	*/
	setInterpolationStrategyParams(params) {
		this.transposer.setInterpolationStrategyParams(params);
	}
	/**
	* Applies a partial set of WSOLA timing parameters to the stretch stage.
	*
	* @remarks
	* Delegates directly to {@link Stretch.setStretchParameters}. Only the provided
	* fields are updated; omitted fields remain unchanged. Pass `sequenceMs: 0` or
	* `seekWindowMs: 0` to switch that dimension back to auto-calculation.
	*
	* @param params Partial set of WSOLA timing parameters to apply.
	*
	* @example
	* st.setStretchParameters({ overlapMs: 12, quickSeek: false });
	*/
	setStretchParameters(params) {
		this.stretch.setStretchParameters(params);
	}
	/**
	* Sets the pitch multiplier and recomputes the derived pipeline rate and tempo.
	*
	* @remarks
	* Internally sets `_rate = pitch` and `_tempo = 1 / pitch`, rewiring the
	* Transposer→Stretch stage order when pitch > 1.
	*/
	set pitch(pitch) {
		this.virtualPitch = pitch;
		this.calculateEffectiveRateAndTempo();
	}
	/**
	* Sets pitch by octave offset.
	*/
	set pitchOctaves(pitchOctaves) {
		this.pitch = Math.exp(.69314718056 * pitchOctaves);
		this.calculateEffectiveRateAndTempo();
	}
	/**
	* Sets pitch by semitone offset.
	*/
	set pitchSemitones(pitchSemitones) {
		this.pitchOctaves = pitchSemitones / 12;
	}
	/**
	* Input buffer for upstream interleaved stereo frames.
	* @returns The input buffer for writing audio frames.
	*/
	get inputBuffer() {
		return this._inputBuffer;
	}
	/**
	* Output buffer that downstream consumers read from.
	* @returns The output buffer for reading processed audio frames.
	*/
	get outputBuffer() {
		return this._outputBuffer;
	}
	/**
	* Recomputes the effective pipeline rate/tempo from `virtualPitch` and rewires stage order when needed.
	*
	* @remarks
	* `_rate` is set to `virtualPitch`; `_tempo` to `1 / virtualPitch`. When `_rate > 1` the
	* Stretch stage feeds the Transposer; otherwise the order is reversed.
	*/
	calculateEffectiveRateAndTempo() {
		const previousTempo = this._tempo;
		const previousRate = this._rate;
		this._tempo = 1 / this.virtualPitch;
		this._rate = this.virtualPitch;
		if (isFloatDifferent(this._tempo, previousTempo)) this.stretch.tempo = this._tempo;
		if (isFloatDifferent(this._rate, previousRate)) this.transposer.rate = this._rate;
		if (this._rate > 1) {
			if (this._outputBuffer !== this.transposer.outputBuffer) {
				this.stretch.inputBuffer = this._inputBuffer;
				this.stretch.outputBuffer = this._intermediateBuffer;
				this.transposer.inputBuffer = this._intermediateBuffer;
				this.transposer.outputBuffer = this._outputBuffer;
			}
		} else if (this._outputBuffer !== this.stretch.outputBuffer) {
			this.transposer.inputBuffer = this._inputBuffer;
			this.transposer.outputBuffer = this._intermediateBuffer;
			this.stretch.inputBuffer = this._intermediateBuffer;
			this.stretch.outputBuffer = this._outputBuffer;
		}
	}
	/**
	* Runs one processing step through the currently selected stage order.
	* @remarks
	* Processes available frames through the pipeline, updating output buffers.
	*/
	process() {
		if (this._rate > 1) {
			this.stretch.process();
			this.transposer.process();
		} else {
			this.transposer.process();
			this.stretch.process();
		}
	}
};
//#endregion
//#region ../worklet-base/src/SoundTouchProcessorBase.ts
/**
* Abstract base class for all SoundTouchJS AudioWorklet processor implementations.
*
* @remarks
* Centralises shared state (pipe, sample buffers, runtime-update queue), the
* `applyPendingRuntimeUpdates` helper, and the core DSP pipeline in
* `processCore`. Subclasses must implement `process` and `onProcessComplete`.
*
* The default `process` implementation calls `applyPendingRuntimeUpdates`,
* `processCore`, and then `onProcessComplete`. Subclasses that need to
* interleave additional logic (e.g. LPC analysis) can override `beforePipeProcess`
* and/or `extractSamples` instead of overriding `process` entirely.
*/
var SoundTouchProcessorBase = class extends AudioWorkletProcessor {
	/** The SoundTouch DSP pipeline instance. */
	_pipe;
	/** Interleaved (L, R, L, R, …) input staging buffer. */
	_samples;
	/** Interleaved output staging buffer populated by `extractSamples`. */
	_outputSamples;
	/** Cumulative count of render blocks where the output buffer ran short. */
	_underrunCount = 0;
	/** Total render blocks processed since construction. */
	_blockCount = 0;
	_pendingInterpolationStrategy = null;
	_pendingInterpolationStrategyParams = null;
	_pendingStretchParameters = null;
	/** Label used in console messages (e.g. `'[SoundTouchProcessor]'`). */
	processorLabel;
	/**
	* Validates and resolves an interpolation strategy id, falling back to
	* `'lanczos'` if the id is unrecognised.
	*
	* @remarks
	* Call this as a static expression inside the `super()` argument list of a
	* subclass constructor so that strategy resolution happens before pipe creation.
	*
	* @param strategy - The strategy id provided by the caller.
	* @param processorLabel - Label included in the fallback console message.
	* @returns The original `strategy` if valid, or `'lanczos'` as a fallback.
	*/
	static resolveStrategy(strategy, processorLabel) {
		try {
			if (strategy) resolveInterpolationStrategy(strategy);
			return strategy;
		} catch {
			console.info(`${processorLabel} Unknown interpolation strategy id:`, strategy, "— falling back to lanczos.");
			return "lanczos";
		}
	}
	/**
	* @param processorLabel - Label string used in diagnostic messages.
	* @param pipeOptions - Options forwarded to the `SoundTouch` constructor. The
	*   `sampleRate` global must be available in the AudioWorklet scope.
	*/
	constructor(processorLabel, pipeOptions) {
		super();
		this.processorLabel = processorLabel;
		this._pipe = new SoundTouch(pipeOptions);
		this._samples = new Float32Array(256);
		this._outputSamples = new Float32Array(256);
		const port = this.port;
		if (port !== void 0) port.onmessage = (event) => {
			const message = event.data;
			if (message.type === "set-interpolation-strategy") {
				this._pendingInterpolationStrategy = message.strategy;
				return;
			}
			if (message.type === "set-interpolation-strategy-params") {
				this._pendingInterpolationStrategyParams = message.params;
				return;
			}
			if (message.type === "set-stretch-parameters") this._pendingStretchParameters = message.params;
		};
	}
	/**
	* Flushes any pending interpolation-strategy or stretch-parameter change
	* that arrived via `port.onmessage` since the last render block.
	*
	* @remarks
	* Call this at the top of `process` before touching `_pipe`.
	*/
	applyPendingRuntimeUpdates() {
		if (this._pendingInterpolationStrategy !== null) {
			try {
				this._pipe.setInterpolationStrategy(this._pendingInterpolationStrategy);
			} catch {
				console.info(`${this.processorLabel} Failed to switch interpolation strategy:`, this._pendingInterpolationStrategy);
			}
			this._pendingInterpolationStrategy = null;
		}
		if (this._pendingInterpolationStrategyParams !== null) {
			try {
				this._pipe.setInterpolationStrategyParams(this._pendingInterpolationStrategyParams);
			} catch {
				console.info(`${this.processorLabel} Failed to update interpolation strategy params.`);
			}
			this._pendingInterpolationStrategyParams = null;
		}
		if (this._pendingStretchParameters !== null) {
			try {
				this._pipe.setStretchParameters(this._pendingStretchParameters);
			} catch {
				console.info(`${this.processorLabel} Failed to update stretch parameters.`);
			}
			this._pendingStretchParameters = null;
		}
	}
	/**
	* Optional hook called after input routing and buffer resize but **before**
	* the SoundTouch pipe processes the block.
	*
	* @remarks
	* Override in subclasses that need to inspect or transform the raw input
	* before it enters the DSP pipeline (e.g. computing LPC coefficients).
	*
	* @param _leftInput - Left-channel input for this render block.
	* @param _rightInput - Right-channel input (same as left for mono sources).
	* @param _frameCount - Number of frames in this block.
	* @param _parameters - AudioParam k-rate values for this render block.
	*/
	beforePipeProcess(_leftInput, _rightInput, _frameCount, _parameters) {}
	/**
	* Extracts rendered frames from the output buffer, writes them to
	* `leftOutput`/`rightOutput`, zero-fills any gap, and returns RMS/peak metrics.
	*
	* @remarks
	* Override in subclasses that apply post-extraction transforms (e.g. formant
	* correction). Overrides are responsible for the full extraction, write-back,
	* silence fill, and returning `{ outputRms, outputPeak }`.
	*
	* @param leftOutput - Destination view for the left channel.
	* @param rightOutput - Destination view for the right channel.
	* @param frameCount - Total frames expected in this block.
	* @param toExtract - Frames available to extract (≤ frameCount).
	* @param _parameters - AudioParam k-rate values (available for overrides).
	* @returns RMS and peak of the extracted block.
	*/
	extractSamples(leftOutput, rightOutput, frameCount, toExtract, _parameters) {
		let outputRms = 0;
		let outputPeak = 0;
		if (toExtract > 0) {
			const extracted = this._outputSamples;
			this._pipe.outputBuffer.extract(extracted, 0, toExtract);
			this._pipe.outputBuffer.receive(toExtract);
			let sumSq = 0;
			let peak = 0;
			for (let i = 0; i < toExtract; i++) {
				const l = extracted[i * 2];
				const r = extracted[i * 2 + 1];
				leftOutput[i] = Number.isFinite(l) ? l : 0;
				rightOutput[i] = Number.isFinite(r) ? r : 0;
				sumSq += l * l + r * r;
				peak = Math.max(peak, Math.abs(l), Math.abs(r));
			}
			outputRms = Math.sqrt(sumSq / (toExtract * 2));
			outputPeak = peak;
		}
		for (let i = toExtract; i < frameCount; i++) {
			leftOutput[i] = 0;
			rightOutput[i] = 0;
		}
		return {
			outputRms,
			outputPeak
		};
	}
	/**
	* Runs the full DSP pipeline for one render block and returns metrics.
	*
	* @remarks
	* Handles input routing, buffer resize, `beforePipeProcess`, pitch
	* calculation, sample interleaving, pipe feed/process, counter updates,
	* and `extractSamples`. Returns `null` when the input is empty or
	* the output has not been allocated, keeping the processor alive.
	*
	* @param inputs - AudioWorklet input buses.
	* @param outputs - AudioWorklet output buses.
	* @param parameters - k-rate AudioParam values.
	* @returns Render-block result, or `null` if inputs are not ready.
	*/
	processCore(inputs, outputs, parameters) {
		const input = inputs[0];
		const output = outputs[0];
		if (!input || !input.length || !output[0] || !output[0].length) return null;
		const leftInput = input[0];
		const rightInput = input.length > 1 ? input[1] : input[0];
		const leftOutput = output[0];
		const rightOutput = output.length > 1 ? output[1] : output[0];
		const frameCount = leftInput.length;
		if (this._samples.length < frameCount * 2) {
			this._samples = new Float32Array(frameCount * 2);
			this._outputSamples = new Float32Array(frameCount * 2);
		}
		this.beforePipeProcess(leftInput, rightInput, frameCount, parameters);
		const pitch = parameters["pitch"][0];
		const pitchSemitones = parameters["pitchSemitones"][0];
		const playbackRate = parameters["playbackRate"][0];
		this._pipe.pitch = pitch * Math.pow(2, pitchSemitones / 12) / playbackRate;
		const samples = this._samples;
		for (let i = 0; i < frameCount; i++) {
			samples[i * 2] = leftInput[i];
			samples[i * 2 + 1] = rightInput[i];
		}
		this._pipe.inputBuffer.putSamples(samples, 0, frameCount);
		this._pipe.process();
		const available = this._pipe.outputBuffer.frameCount;
		const toExtract = Math.min(available, frameCount);
		this._blockCount++;
		if (available < frameCount) this._underrunCount++;
		const { outputRms, outputPeak } = this.extractSamples(leftOutput, rightOutput, frameCount, toExtract, parameters);
		return {
			frameCount,
			toExtract,
			available,
			leftInput,
			rightInput,
			leftOutput,
			rightOutput,
			outputRms,
			outputPeak
		};
	}
	/**
	* AudioWorkletProcessor render callback. Keeps the processor alive by always returning `true`.
	*
	* @remarks
	* The default implementation calls `applyPendingRuntimeUpdates`, `processCore`,
	* and `onProcessComplete`. Override only when the execution order must differ
	* (e.g. pre-pipe analysis steps not covered by `beforePipeProcess`).
	*
	* @param inputs - AudioWorklet input buses.
	* @param outputs - AudioWorklet output buses.
	* @param parameters - k-rate AudioParam values.
	* @returns Always `true` to keep the processor alive.
	*/
	process(inputs, outputs, parameters) {
		this.applyPendingRuntimeUpdates();
		const result = this.processCore(inputs, outputs, parameters);
		if (result !== null) this.onProcessComplete(result);
		return true;
	}
};
//#endregion
//#region ../worklet-base/src/types.ts
/**
* Standard pitch, pitchSemitones, and playbackRate AudioParam descriptors
* shared by all SoundTouchJS worklet processors.
*/
var STANDARD_PARAMETER_DESCRIPTORS = [
	{
		name: "pitch",
		defaultValue: 1,
		minValue: .1,
		maxValue: 8,
		automationRate: "k-rate"
	},
	{
		name: "pitchSemitones",
		defaultValue: 0,
		minValue: -24,
		maxValue: 24,
		automationRate: "k-rate"
	},
	{
		name: "playbackRate",
		defaultValue: 1,
		minValue: .1,
		maxValue: 8,
		automationRate: "k-rate"
	}
];
//#endregion
//#region src/processor.ts
var PROCESSOR_NAME = "soundtouch-processor";
/**
* Audio render-thread processor that applies SoundTouch transformations to stereo blocks.
*
* @remarks
* Receives audio from the main thread, applies pitch, tempo, and rate transformations,
* and outputs processed stereo audio. Handles runtime strategy switching via messages.
*/
var SoundTouchProcessor = class extends SoundTouchProcessorBase {
	/** Static AudioParam metadata consumed by the browser. */
	static get parameterDescriptors() {
		return STANDARD_PARAMETER_DESCRIPTORS;
	}
	/**
	* @param options Worklet constructor options provided by the main thread.
	*
	* @remarks
	* Unknown interpolation strategy ids are logged and coerced to `lanczos`
	* so render-thread startup remains resilient.
	*/
	constructor(options) {
		super("[SoundTouchProcessor]", {
			sampleRate,
			sampleBufferType: options?.processorOptions?.sampleBufferType ?? "circular",
			interpolationStrategy: SoundTouchProcessorBase.resolveStrategy(options?.processorOptions?.interpolationStrategy, "[SoundTouchProcessor]")
		});
	}
	onProcessComplete(result) {
		if (this._blockCount % 100 === 0) this.port.postMessage({
			type: "metrics",
			framesBuffered: result.available,
			underrunCount: this._underrunCount,
			blockCount: this._blockCount,
			outputRms: result.outputRms,
			outputPeak: result.outputPeak
		});
	}
};
registerProcessor(PROCESSOR_NAME, SoundTouchProcessor);
//#endregion

//# sourceMappingURL=soundtouch-processor.js.map