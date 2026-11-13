/** Reserve controls only after the draft, leaving earlier visual rows full width. */
import { StateField } from '@codemirror/state'
import { Decoration, EditorView, ViewPlugin, WidgetType } from '@codemirror/view'

class ActionSpace extends WidgetType {
  eq() { return true }
  toDOM() {
    const space = document.createElement('span')
    space.className = 'cm-composer-action-space'
    space.setAttribute('aria-hidden', 'true')
    return space
  }
}

export const composerActionSpace = StateField.define({
  create: state => actionSpace(state.doc),
  update: (value, transaction) => transaction.docChanged ? actionSpace(transaction.newDoc) : value,
  provide: field => EditorView.decorations.from(field),
})
function actionSpace(doc) {
  const lastLine = doc.line(doc.lines)
  const ranges = [Decoration.widget({ widget: new ActionSpace(), side: 1 }).range(doc.length)]
  // An empty line has no text glyph to size the native caret. A tall inline
  // widget would become its caret height; use ordinary line padding instead.
  if (!lastLine.length) ranges.push(Decoration.line({ class: 'cm-composer-empty-tail' }).range(lastLine.from))
  return Decoration.set(ranges, true)
}

// Long drafts get a separate action footer, so scrolling never puts text
// underneath a fixed control. Measure natural content against the unchanged
// row cap, not the reduced scroller height, to avoid a layout feedback loop.
const wrappingGeometry = ViewPlugin.fromClass(class {
  constructor(view) {
    this.view = view
    this.measure = {
      read: () => {
        const row = view.dom.closest('.chat__input-line')
        const actions = row?.querySelector('.chat__input-actions')
        if (!row || !actions || !row.clientWidth) return null
        return { row, width: actions.offsetWidth, height: actions.offsetHeight,
          cap: parseFloat(getComputedStyle(row).maxHeight), content: view.contentDOM.scrollHeight }
      },
      write: size => {
        if (!size) return
        const { row, width, height, cap, content } = size
        row.style.setProperty('--composer-actions-width', `${width}px`)
        row.style.setProperty('--composer-actions-height', `${height}px`)
        row.toggleAttribute('data-composer-scrolls', Number.isFinite(cap) && content > cap + 1)
        const actions = row.querySelector('.chat__input-actions')
        if (actions !== this.actions) {
          if (this.actions) this.observer?.unobserve(this.actions)
          this.actions = actions
          this.observer?.observe(actions)
        }
      },
    }
    this.observer = typeof ResizeObserver === 'undefined' ? null
      : new ResizeObserver(() => view.requestMeasure(this.measure))
    this.observer?.observe(view.contentDOM)
    this.observer?.observe(view.dom)
    view.requestMeasure(this.measure)
  }
  update(update) {
    if (update.docChanged || update.geometryChanged) this.view.requestMeasure(this.measure)
  }
  destroy() { this.observer?.disconnect() }
})

export const composerWrapping = [composerActionSpace, wrappingGeometry]
