/* This tab's shared-folder state (folders, activity), re-rendering on every change. */
import { useSyncExternalStore } from 'react'
import { getSharedFoldersState, subscribeSharedFolders } from '../lib/sharedFolders/hostState.js'

export default function useSharedFolders() {
  return useSyncExternalStore(subscribeSharedFolders, getSharedFoldersState, getSharedFoldersState)
}
