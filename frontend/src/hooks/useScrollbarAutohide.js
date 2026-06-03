import { useEffect } from 'react'

export default function useScrollbarAutohide() {
  useEffect(() => {
    let t
    const onScroll = () => {
      document.documentElement.classList.add('is-scrolling')
      clearTimeout(t)
      t = setTimeout(() => {
        document.documentElement.classList.remove('is-scrolling')
      }, 900)
    }

    window.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => {
      window.removeEventListener('scroll', onScroll, { capture: true })
      clearTimeout(t)
    }
  }, [])
}
