/** Controlled Markdown editor; the source stays plain Markdown while its presentation follows the caret. */
import { useLayoutEffect, useRef } from 'react'
import { EditorState, Prec, Compartment } from '@codemirror/state'
import { EditorView, keymap, placeholder } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { composerMarkdown } from './composerMarkdown.js'
import { syncComposerTallState } from './composerTextareaSizing.js'
import './MarkdownComposer.css'

export default function MarkdownComposer({ inputRef, value, onChange, onInputIntent, onKeyDown, onKeyUp,
  onPaste, onFocus, onBlur, spellCheck, ...attributes }) {
  const hostRef = useRef(null)
  const viewRef = useRef(null)
  const attributesRef = useRef(new Compartment())
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
      history(), keymap.of([...historyKeymap, ...defaultKeymap]),
      EditorView.lineWrapping, placeholder('Message Möbius…'), composerMarkdown,
      Prec.highest(EditorView.domEventHandlers(handlers)),
      attributesRef.current.of(EditorView.contentAttributes.of(JSON.parse(contentAttributes))),
    ]
    const createState = text => EditorState.create({ doc: text, selection: { anchor: text.length }, extensions })
    const view = new EditorView({
      state: createState(props.current.value), parent: hostRef.current,
      dispatchTransactions(transactions, editor) {
        // Paste/formatting/undo can be editor transactions without a DOM input
        // event. Renew follow-bottom before their layout reaches the footer.
        if (transactions.some(transaction => transaction.docChanged)) {
          // Commit the controlled draft (including the voice baseline) before
          // handing scroll ownership over, and before the editor's DOM grows.
          props.current.onChange?.(transactions.at(-1).state.doc.toString())
          props.current.onInputIntent?.({ type: 'input', target: editor.contentDOM })
        }
        editor.update(transactions)
        if (typeof ResizeObserver === 'undefined') {
          editor.requestMeasure({
            read: () => editor.dom.offsetHeight,
            write: height => syncComposerTallState(editor.contentDOM, height),
          })
        }
      },
    })
    viewRef.current = { view, createState }
    inputRef.current = view.contentDOM
    const observer = typeof ResizeObserver === 'undefined' ? null
      : new ResizeObserver(() => syncComposerTallState(view.contentDOM, view.dom.offsetHeight))
    observer?.observe(view.dom)
    return () => {
      observer?.disconnect()
      if (inputRef.current === view.contentDOM) inputRef.current = null
      viewRef.current = null
      view.destroy()
    }
    // The editor lives as long as this chat. Current callbacks come through props.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputRef])

  useLayoutEffect(() => {
    const current = viewRef.current
    if (!current || current.view.state.doc.toString() === value) return
    // A send, history entry, restored draft, or voice replacement is a new
    // editing context. Old undo entries must never resurrect a sent message.
    current.view.setState(current.createState(value))
    current.view.dispatch({ effects: attributesRef.current.reconfigure(EditorView.contentAttributes.of(JSON.parse(contentAttributes))) })
  }, [value, contentAttributes])

  useLayoutEffect(() => {
    viewRef.current?.view.dispatch({ effects: attributesRef.current.reconfigure(EditorView.contentAttributes.of(JSON.parse(contentAttributes))) })
  }, [contentAttributes])

  return <div ref={hostRef} className="chat__input chat__markdown-input" spellCheck={spellCheck} />
}
