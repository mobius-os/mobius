import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'

const commonRules = {
  ...js.configs.recommended.rules,
  // The codebase intentionally uses catch-and-degrade boundaries extensively.
  'no-empty': ['error', { allowEmptyCatch: true }],
  // Existing modules still carry some owner-facing extension seams. Enabling
  // this before those APIs are narrowed would turn static analysis into churn;
  // correctness checks remain hard failures.
  'no-unused-vars': 'off',
  'no-useless-escape': 'off',
  'no-useless-assignment': 'off',
  'no-regex-spaces': 'off',
  'no-control-regex': 'off',
  'preserve-caught-error': 'off',
}

export default [
  {
    ignores: [
      'dist/**',
      '.dist-*/**',
      '.assets-attic/**',
      'node_modules/**',
      'public/vendor/**',
      'public/mobius-runtime.js',
      // Vendored upstream build (SoundTouchJS, MPL-2.0) pinned by version and
      // digest in src/lib/speech/speechPitchAsset.js. It stays beside the other
      // speech assets rather than moving under public/vendor/ because /vendor/*
      // is served CacheFirst as an immutable lib, and a worklet is a worker-
      // class script that must keep revalidating (see scripts/precache-policy.mjs).
      // Its bytes are digest-checked, so it cannot carry an inline globals
      // annotation either: exclude it rather than lint code we must not edit.
      'public/speech/soundtouch-processor.js',
    ],
  },
  {
    files: [
      'src/**/*.{js,jsx}',
      'scripts/**/*.mjs',
      'vite.config.js',
      // Served verbatim from public/ rather than bundled, but still hand-
      // written source that deserves the same rules and worker globals. A
      // pattern, not a filename: the generated files here are already in
      // `ignores` above, so the next hand-written one is linted by default
      // instead of shipping unchecked.
      'public/**/*.js',
    ],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: {
        ...globals.browser,
        ...globals.node,
        ...globals.serviceworker,
      },
    },
    linterOptions: {
      reportUnusedDisableDirectives: 'error',
    },
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      ...commonRules,
      'react-hooks/rules-of-hooks': 'error',
      // Existing state-owner extractions use deliberate ref-backed dependency
      // boundaries. Surface new review candidates without making that migration
      // a prerequisite for correctness linting.
      'react-hooks/exhaustive-deps': 'warn',
    },
  },
]
