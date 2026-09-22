/**
 * Keep a touch on a composer-adjacent action from blurring the textarea and
 * moving the target before activation. Mouse and keyboard activation retain
 * the button's native click path. Only controls that can move before Safari's
 * delayed click opt into immediate touchend activation.
 */
export function composerAdjacentActionProps(
  activate,
  { activateOnTouchEnd = false } = {},
) {
  const props = {
    onPointerDown(event) {
      if (event.pointerType === 'touch') event.preventDefault()
    },
    onClick() {
      activate()
    },
  }
  if (activateOnTouchEnd) {
    props.onTouchEnd = event => {
      event.preventDefault()
      activate()
    }
  }
  return props
}
