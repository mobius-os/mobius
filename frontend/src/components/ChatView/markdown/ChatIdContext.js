import { createContext, useContext } from 'react'

export const ChatIdContext = createContext(null)

export function useChatId() {
  return useContext(ChatIdContext)
}
