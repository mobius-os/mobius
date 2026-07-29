/* Preserve native text selection inside controls without firing their action on pointer release. */

export function textSelectionSnapshot(
  selection = globalThis.getSelection?.(),
) {
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
    return null
  }
  return [
    selection.anchorNode,
    selection.anchorOffset,
    selection.focusNode,
    selection.focusOffset,
  ]
}

function selectionSnapshotsMatch(left, right) {
  return left === right || (
    !!left
    && !!right
    && left.every((value, index) => value === right[index])
  )
}

export function pointerSelectionChangedWithin(
  selectionBeforePointer,
  node,
  selection = globalThis.getSelection?.(),
) {
  if (
    !node
    || !selection
    || selection.isCollapsed
    || selection.rangeCount === 0
    || selectionSnapshotsMatch(
      selectionBeforePointer,
      textSelectionSnapshot(selection),
    )
  ) return false

  try {
    return selection.getRangeAt(0).intersectsNode(node)
  } catch {
    return false
  }
}
