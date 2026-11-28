/** Controlled Markdown editor; the source stays plain Markdown while its presentation follows the caret. */
import { useLayoutEffect, useRef } from 'react'
import { Annotation, EditorState, Prec, Compartment, Transaction } from '@codemirror/state'
import { EditorView, keymap, placeholder } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { composerMarkdown } from './composerMarkdown.js'
import { syncComposerTallState } from './composerTextareaSizing.js'
import './MarkdownComposer.css'

// Marks a value set from outside (send, history, restored draft, voice) so
// it is not reported back as typing.
const controlledValue = Annotation.define()

/**
 * Scroll only the editor's own scroller so the caret is visible.
 *
 * CodeMirror reveals the caret for typing, but a value or caret placed from
 * outside (a restored draft on return to a chat, a sent message cleared, a
 * caret moved to the end) and a box that shrinks under it (the keyboard
 * opening halves the room) leave the scroller where it was: the box showed
 * the top of a long draft while the caret sat below its fold. Its own
 * scrollIntoView effect would also scroll the shell's fixed layers, so only
 * the editor scroller moves here.
 */
export function revealComposerCaret(view) {
  if (!view || view.destroyed) return
  view.requestMeasure({
    key: 'composer-reveal-caret',
    read(editor) {
      const scroller = editor.scrollDOM
      if (!scroller.clientHeight) return null
      const head = editor.state.selection.main.head
      return {
        atEnd: head === editor.state.doc.length,
        caret: editor.coordsAtPos(head),
        box: scroller.getBoundingClientRect(),
        top: scroller.scrollTop,
        // The content's own padding stays visible below the last line.
        pad: parseFloat(getComputedStyle(editor.contentDOM).paddingBottom) || 0,
      }
    },
    write(measured, editor) {
      if (!measured) return
      const scroller = editor.scrollDOM
      const { atEnd, caret, box, top, pad } = measured
      if (!caret) {
        if (atEnd) scroller.scrollTop = scroller.scrollHeight
        return
      }
      if (caret.bottom + pad > box.bottom) scroller.scrollTop = top + caret.bottom + pad - box.bottom
      else if (caret.top < box.top) scroller.scrollTop = top - (box.top - caret.top)
    },
  })
}

export default function MarkdownComposer({ inputRef, value, onChange, onInputIntent, onKeyDown, onKeyUp,
  onPaste, onFocus, onBlur, spellCheck, ...attributes }) {
  const hostRef = useRef(null)
  const viewRef = useRef(null)
  const attributesRef = useRef(new Compartment())
  const historyRef = useRef(new Compartment())
  const followCaretRef = useRef(true)
  const props = useRef(null)
  props.current = { value, onChange, onInputIntent, onKeyDown, onKeyUp, onPaste, onFocus, onBlur }
  const makeAttributes = () => ({
    class: 'chat__editor-content', role: 'textbox', 'aria-multiline': 'true',
    'aria-label': 'Message Möbius…', spellcheck: String(spellCheck), autocapitalize: 'sentences',
    ...Object.fromEntries(Object.entries(attributes).filter(([name, val]) => name.startsWith('aria-') && val != null)),
    ...(attributes.role ? { role: attributes.role } : {}),
  })
  const contentAttributes = JSON.stringify(makeAttributes())

  useLayoutEffect(() => {
    const handlers = {}
    for (const [name, prop] of Object.entries({ keydown: 'onKeyDown', keyup: 'onKeyUp', paste: 'onPaste', focus: 'onFocus', blur: 'onBlur' })) {
      handlers[name] = event => {
        props.current[prop]?.(event)
        return event.defaultPrevented
      }
    }
    const extensions = [
      markdown({ base: markdownLanguage, addKeymap: false }),
      historyRef.current.of(history()), keymap.of([...historyKeymap, ...defaultKeymap]),
      EditorView.lineWrapping, placeholder('Message Möbius…'), composerMarkdown,
      Prec.highest(EditorView.domEventHandlers(handlers)),
      attributesRef.current.of(EditorView.contentAttributes.of(JSON.parse(contentAttributes))),
    ]
    const initial = props.current.value
    const view = new EditorView({
      state: EditorState.create({ doc: initial, selection: { anchor: initial.length }, extensions }),
      parent: hostRef.current,
      dispatchTransactions(transactions, editor) {
        // Paste/formatting/undo can be editor transactions without a DOM input
        // event. Renew follow-bottom before their layout reaches the footer.
        if (transactions.some(transaction => transaction.docChanged && !transaction.annotation(controlledValue))) {
          // Commit the controlled draft (including the voice baseline) before
          // handing scroll ownership over, and before the editor's DOM grows.
          props.current.onChange?.(transactions.at(-1).state.doc.toString())
          props.current.onInputIntent?.({ type: 'input', target: editor.contentDOM })
        }
        editor.update(transactions)
        // Editing or moving the caret means the owner is working at it again.
        // A value or caret set from outside carries no user event and no
        // scroll request of its own, so reveal it here.
        if (transactions.some(transaction => transaction.selection || transaction.docChanged)) {
          followCaretRef.current = true
          if (transactions.some(transaction => (transaction.selection || transaction.docChanged)
              && !transaction.scrollIntoView && !transaction.annotation(Transaction.userEvent))) {
            revealComposerCaret(editor)
          }
        }
        if (typeof ResizeObserver === 'undefined') {
          editor.requestMeasure({
            read: () => editor.dom.offsetHeight,
            write: height => syncComposerTallState(editor.contentDOM, height),
          })
        }
      },
    })
    viewRef.current = view
    inputRef.current = view.contentDOM
    // A remount (returning to this chat) starts at the end of the draft; show
    // that end rather than the top of a long draft.
    followCaretRef.current = true
    revealComposerCaret(view)
    // The keyboard opening or closing, an attachment, or a split pane resizes
    // the box under the caret. Keep the caret in view when the owner was at
    // it; a scroll they made away from it to read earlier text is respected.
    let boxHeight = view.scrollDOM.clientHeight
    const onScroll = () => {
      const scroller = view.scrollDOM
      // A scroll caused by the box itself resizing is not a reading choice.
      if (scroller.clientHeight !== boxHeight) return
      const caret = view.coordsAtPos(view.state.selection.main.head)
      if (!caret) { followCaretRef.current = false; return }
      const box = scroller.getBoundingClientRect()
      followCaretRef.current = caret.bottom <= box.bottom + 1 && caret.top >= box.top - 1
    }
    view.scrollDOM.addEventListener('scroll', onScroll, { passive: true })
    const observer = typeof ResizeObserver === 'undefined' ? null
      : new ResizeObserver(() => {
        syncComposerTallState(view.contentDOM, view.dom.offsetHeight)
        const height = view.scrollDOM.clientHeight
        if (height === boxHeight) return
        boxHeight = height
        if (followCaretRef.current) revealComposerCaret(view)
      })
    observer?.observe(view.dom)
    return () => {
      view.scrollDOM.removeEventListener('scroll', onScroll)
      observer?.disconnect()
      if (inputRef.current === view.contentDOM) inputRef.current = null
      viewRef.current = null
      view.destroy()
    }
    // The editor lives as long as this chat. Current callbacks come through props.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputRef])

  useLayoutEffect(() => {
    const view = viewRef.current
    if (!view || view.state.doc.toString() === value) return
    // A send, history entry, restored draft, or voice replacement is a new
    // editing context. Old undo entries must never resurrect a sent message,
    // so the edit lands with undo history switched off and then a fresh one.
    // It must be an ordinary transaction, never EditorView.setState: Android
    // Chrome types through an EditContext that only transactions keep in
    // sync, and after setState every keystroke targets the old text and is
    // dropped until the box is refocused.
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: value },
      selection: { anchor: value.length },
      effects: historyRef.current.reconfigure([]),
      annotations: controlledValue.of(true),
    })
    view.dispatch({ effects: historyRef.current.reconfigure(history()), annotations: controlledValue.of(true) })
    // An Android keyboard usually still has the last word open (composing)
    // when Send is tapped. The replacement does not end that composition, so
    // the next word is merged into stale positions. Reattaching the
    // EditContext ends it, as refocusing would, without hiding the keyboard.
    const context = view.contentDOM.editContext
    if (context && view.compositionStarted) {
      view.contentDOM.editContext = null
      view.contentDOM.editContext = context
    }
  }, [value])

  useLayoutEffect(() => {
    viewRef.current?.dispatch({ effects: attributesRef.current.reconfigure(EditorView.contentAttributes.of(JSON.parse(contentAttributes))) })
  }, [contentAttributes])

  return <div ref={hostRef} className="chat__input chat__markdown-input" spellCheck={spellCheck} />
}
