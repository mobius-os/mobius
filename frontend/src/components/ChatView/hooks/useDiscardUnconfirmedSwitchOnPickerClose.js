import { useEffect, useRef } from 'react'
import { clearProviderSwitch } from '../providerSwitch.js'

/**
 * Leaving the model picker with an unconfirmed cross-provider switch staged is
 * a no-op: discard it so reopening the picker shows the current model, not a
 * lingering "confirm?" for a model the owner picked but never confirmed.
 *
 * `ComposerPopover` stays mounted across the unified brain menu's open/close
 * (only the panel unmounts), so `pickerOpen` becoming false is the reliable
 * signal that the picker closed. Only the staged `'confirming'` state is
 * dropped — an in-flight `'switching'` or a committed `'success'` is left
 * alone, and entering the picker or any transition without a staged switch is
 * a no-op. Chat navigation remounts this component, so it is not a picker
 * close and keeps its existing restore behavior.
 *
 * @param {boolean} pickerOpen - whether the unified composer picker is open.
 * @param {string|undefined} providerSwitchStatus - the staged switch status for
 *   this chat (`'confirming' | 'switching' | 'success' | 'error' | 'idle'`).
 * @param {string} chatId - the chat whose staged switch is discarded.
 */
export default function useDiscardUnconfirmedSwitchOnPickerClose(
  pickerOpen,
  providerSwitchStatus,
  chatId,
) {
  const wasOpenRef = useRef(pickerOpen)
  useEffect(() => {
    const closedPicker = wasOpenRef.current && !pickerOpen
    wasOpenRef.current = pickerOpen
    if (closedPicker && providerSwitchStatus === 'confirming') {
      clearProviderSwitch(chatId)
    }
  }, [pickerOpen, providerSwitchStatus, chatId])
}
