/** Test-only browser bridge to the public editor API; never imported by the served shell. */
import { editorText, editorSelection, selectEditorRange } from '../textEditor.js'
globalThis.__mobiusComposerTest = { text: editorText, selection: editorSelection, select: selectEditorRange }
