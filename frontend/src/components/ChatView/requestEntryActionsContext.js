// What a request-list card's own entries do beyond the shared Submit: press
// Restart now, and record a state an entry settled on its own in the chat's
// transcript (and so its cached detail). Provided by ChatView.
import { createContext } from 'react'

export const RequestEntryActionsContext = createContext(null)
