// Whether this ChatView copy is off screen. The workspace keeps a copy of a
// chat mounted in each world (Standard and Builder), so the same card can be
// mounted twice. Only the copy on screen owns its unsent input; a hidden copy
// must neither write its stale state over that draft nor keep showing it once
// revealed (see QuestionCard).
import { createContext } from 'react'

export const ChatSurfaceHiddenContext = createContext(false)
