const PAGES_REVISION = '8ae65694efd3658de4cfdbef5fc8aca833248d1c'
const MODEL_REVISION = 'c2d23606a738c5afb5e24e44f9d2f5d6af1b4528'
const VOICE_REVISION = 'e041936c75475d350b405bc870bcf7c22da4e9e6'

const POCKET_TTS_ALBA_PACKAGE = Object.freeze({
  key: 'pocket-tts-alba-xn-q8-worker-v1',
  assets: [
    {
      id: 'runtime-module',
      url: `https://raw.githubusercontent.com/LaurentMazare/LaurentMazare.github.io/${PAGES_REVISION}/pocket-tts/ptts_wasm.js`,
      bytes: 12_706,
      chunks: [{ bytes: 12_706, sha256: 'd2848ed21ccd4b46cf38e0659f85867bc30c8631305de5fd220f0295185c7c61' }],
    },
    {
      id: 'runtime-wasm',
      url: `https://raw.githubusercontent.com/LaurentMazare/LaurentMazare.github.io/${PAGES_REVISION}/pocket-tts/ptts_wasm_bg.wasm`,
      bytes: 952_895,
      chunks: [{ bytes: 952_895, sha256: '809e783f77a62698dbbb2bad9f49ea02212a21732047b4880481d9f7b4c70e78' }],
    },
    {
      id: 'tokenizer',
      url: `https://huggingface.co/kyutai/pocket-tts-without-voice-cloning/resolve/${VOICE_REVISION}/tokenizer.model`,
      bytes: 59_339,
      chunks: [{ bytes: 59_339, sha256: 'd461765ae179566678c93091c5fa6f2984c31bbe990bf1aa62d92c64d91bc3f6' }],
    },
    {
      id: 'model',
      url: `https://huggingface.co/lmz/pocket-tts-without-voice-cloning-q8/resolve/${MODEL_REVISION}/tts_b6369a24.gguf`,
      bytes: 146_499_264,
      chunks: [
        ['009adf6a2b4dacc3c383af3c05d2e77e6359f0b6fb06171e642d0494a86d6ed7'],
        ['230fa3d9d28b2d0272adce5ee060ed883c8a7e4a770909186510be5ccf663220'],
        ['9f63df60a54b62a26faa2ffd21551d12df3a7d7c4c79d7fefb86826f48de2e6f'],
        ['4e6918f80cb4ec163d4aec1fd60e9a85cdfcb17cf822d1efa1682c1ef3d1491e'],
        ['84ff405d48429f5c65f649a942ba8669f2d4807fae44274fa138c7b7e1d8bcc3'],
        ['a0dae791d5ce79316d5b5ed74c39d127b2f4bc9a2ff6b23764ddb5d42bc02b0e'],
        ['174ecfbdef86c6939ea4ad7e0d04d1e6c0fae4e8644784cbd7b74e550cd7e799'],
        ['9be2a47dafe17b99cd0ecd810a43893d9981705d83e98475b4375f8845fcc346'],
        ['5d096efec1610ba2551e93dd6c25188f03b8cf100b1502aa04192e8646901b7a'],
        ['f38fe6215d970d5ad0ee115cf882821d89704cf5e58e98a16c071abfe0f1489b'],
        ['57195622c757c30c6a3700579f5eb372227a998c692e7ce73f7dc0f55e1b396c'],
        ['e8cdfa3cd19d167f497bff7ecec435b215b074b376210d360a6f813c0796cfe7'],
        ['12d20c6e2a6471bd0372b7ecbf2d8169c32f43303339a3e603fd123383290a12'],
        ['58552fff3dec2f500b5befe4393c8ac746781b794b33c907310211004e68415f'],
        ['1c0e09cf240a000b9a12ff1cda9af5873e558b7c7ba2c99507c67b83a676b536'],
        ['c1bd1813c202adaab6791326c5234ee25feb340a42be517c3278b391b7ec6236'],
        ['57c0e10e53b737fb8bfddb25367820bfbaf9f80dc684f32c5a76e3a3e1aff647'],
        ['f05a31260759633313b395c2e5634e4b0d547382f5b417788c772c9ac8ba31a1', 3_892_928],
      ].map(([sha256, bytes = 8_388_608]) => ({ bytes, sha256 })),
    },
    {
      id: 'voice',
      url: `https://huggingface.co/kyutai/pocket-tts-without-voice-cloning/resolve/${VOICE_REVISION}/embeddings_v2/alba.safetensors`,
      bytes: 6_148_328,
      chunks: [{ bytes: 6_148_328, sha256: '413fc94e6bd73e1b6f25e850b25652f5163e41d92a403954f86cbbedd0c414d1' }],
    },
  ],
})

export const SPEECH_MODEL_PARTITION = 'speech-v1'
export const SPEECH_MODEL_STORAGE_LIMITS = Object.freeze({
  max_bytes: 256 * 1024 * 1024,
  max_asset_bytes: 256 * 1024 * 1024,
  max_chunk_bytes: 8 * 1024 * 1024,
})

const models = Object.freeze([
  Object.freeze({
    id: 'pocket-tts-alba',
    name: 'Pocket TTS · Alba',
    engine: 'Pocket TTS',
    voice: 'Alba',
    language: 'English',
    description: 'Private, on-device English voice. Runs after one device download.',
    sampleRate: 24_000,
    storedBytes: 153_672_532,
    package: POCKET_TTS_ALBA_PACKAGE,
  }),
])

export const DEFAULT_SPEECH_MODEL_ID = models[0].id

export function speechModels() {
  return models
}

export function speechModel(modelId) {
  return models.find((model) => model.id === modelId) || null
}

export function publicSpeechModel(model) {
  return model ? {
    id: model.id,
    name: model.name,
    engine: model.engine,
    voice: model.voice,
    language: model.language,
    description: model.description,
    sampleRate: model.sampleRate,
    storedBytes: model.storedBytes,
  } : null
}
