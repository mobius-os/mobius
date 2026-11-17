/* Google and Apple brand marks for "Continue with …" sign-in buttons. */

export function GoogleMark({ size = 18 }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" focusable="false">
      <path fill="#4285f4" d="M21.4 12.2c0-.6-.1-1.2-.2-1.8H12v3.5h5.3a4.5 4.5 0 0 1-2 2.9v2.3h3.2c1.8-1.7 2.9-4.2 2.9-6.9Z" />
      <path fill="#34a853" d="M12 21.7c2.6 0 4.9-.9 6.5-2.4l-3.2-2.3c-.9.6-2 .9-3.3.9-2.6 0-4.7-1.7-5.5-4H3.3v2.3a9.8 9.8 0 0 0 8.7 5.5Z" />
      <path fill="#fbbc05" d="M6.5 14a5.8 5.8 0 0 1 0-3.9V7.8H3.3a9.8 9.8 0 0 0 0 8.5L6.5 14Z" />
      <path fill="#ea4335" d="M12 6.1c1.4 0 2.7.5 3.7 1.4l2.8-2.8A9.4 9.4 0 0 0 12 2.2a9.8 9.8 0 0 0-8.7 5.6l3.2 2.3c.8-2.3 2.9-4 5.5-4Z" />
    </svg>
  )
}

export function AppleMark({ size = 18 }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M17.1 12.5c0-3.2 2.6-4.7 2.7-4.8a5.8 5.8 0 0 0-4.6-2.5c-1.9-.2-3.8 1.2-4.8 1.2s-2.5-1.1-4.2-1.1A6.2 6.2 0 0 0 1 8.5c-2.2 3.9-.6 9.5 1.6 12.6 1.1 1.5 2.3 3.2 4 3.2 1.6-.1 2.2-1 4.1-1s2.5 1 4.2 1c1.7 0 2.8-1.5 3.8-3.1a12.6 12.6 0 0 0 1.8-3.6 5.5 5.5 0 0 1-3.4-5.1ZM13.9 3.2A5.6 5.6 0 0 0 15.2-.8a5.6 5.6 0 0 0-3.7 1.9A5.3 5.3 0 0 0 10.2 5a4.7 4.7 0 0 0 3.7-1.8Z"
        transform="translate(1.2 1) scale(.9)"
      />
    </svg>
  )
}
