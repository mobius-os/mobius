export function useEffect() {}
export function useState(initial) {
  return [typeof initial === 'function' ? initial() : initial, () => {}]
}
