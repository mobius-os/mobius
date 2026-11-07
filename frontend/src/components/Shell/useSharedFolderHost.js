/* Run this tab's shared-folder host for the shell's lifetime, bound to its system stream. */
import { useEffect } from 'react'
import {
  setSharedFolderSubscription,
  startSharedFolderHost,
} from '../../lib/sharedFolders/folderHost.js'

export default function useSharedFolderHost(subscriptionId) {
  useEffect(() => {
    void startSharedFolderHost()
  }, [])
  useEffect(() => {
    void setSharedFolderSubscription(subscriptionId)
  }, [subscriptionId])
}
