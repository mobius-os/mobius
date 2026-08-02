/* Build the display geometry sent to an agent for faithful visual checks. */

function positiveNumber(value, fallback) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : fallback
}

export function agentViewport(windowLike, height) {
  return {
    width: positiveNumber(windowLike?.innerWidth, 1),
    height: positiveNumber(
      height,
      positiveNumber(windowLike?.visualViewport?.height, windowLike?.innerHeight || 1),
    ),
    pixelRatio: positiveNumber(windowLike?.devicePixelRatio, 1),
  }
}
