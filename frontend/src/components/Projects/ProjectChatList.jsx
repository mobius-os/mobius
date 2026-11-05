/* A project's Chats list: every row opens its chat and offers the sidebar's
   chat actions menu through the same right-click, hold, and keyboard gestures. */

import { useEffect, useRef, useState } from 'react'
import { Chat as MessageSquare } from '@openai/apps-sdk-ui/components/Icon'
import DrawerItemActionMenu from '../Drawer/DrawerItemActionMenu.jsx'
import useItemMenuGestures from '../Drawer/useItemMenuGestures.js'
import { drawerNameMaxLength } from '../Drawer/drawerRename.js'
import { useHistoryDismiss } from '../../hooks/useHistoryDismiss.jsx'
import { projectChatStatus } from '../../lib/projectChats.js'
import { writeClipboardText } from '../../runtime/clipboard.js'

function chatLabel(chat) {
  return chat?.title || 'New chat'
}

// Pointer gestures use their real release point; keyboard invocations report
// 0,0 and fall back to the bottom-center of the focused row.
function menuPlacement(point, trigger) {
  const rect = trigger?.getBoundingClientRect?.()
  const hasPoint = Number.isFinite(Number(point?.x))
    && Number.isFinite(Number(point?.y))
    && (Number(point.x) !== 0 || Number(point.y) !== 0)
  return {
    clientX: hasPoint ? Number(point.x) : (rect?.left || 0) + (rect?.width || 0) / 2,
    clientY: hasPoint ? Number(point.y) : rect?.bottom || 0,
  }
}

function ProjectChatRow({ chat, onOpen, onOpenMenu }) {
  const { triggerProps, consumeHeldClick } = useItemMenuGestures(
    (point, trigger, options) => onOpenMenu(chat, point, trigger, options),
  )
  const status = projectChatStatus(chat)
  return (
    <button
      type="button"
      className="project-chats__row"
      onClick={() => {
        if (consumeHeldClick()) return
        onOpen(chat)
      }}
      {...triggerProps}
    >
      <MessageSquare width={16} height={16} aria-hidden="true" />
      <span><strong>{chatLabel(chat)}</strong>{status && <small className={chat.running ? 'project-chats__status--working' : undefined}>{status}</small>}</span>
    </button>
  )
}

export function ProjectChatRename({ chat, onSubmit, onCancel }) {
  const inputRef = useRef(null)
  const settledRef = useRef(false)
  const menuFocusPendingRef = useRef(true)

  function finishMenuFocusHandoff() {
    menuFocusPendingRef.current = false
  }

  // Rename replaces the menu that opened it; focusing a frame later lets that
  // teardown settle so the editor keeps focus.
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      inputRef.current?.focus()
      inputRef.current?.select()
    })
    document.addEventListener('pointerdown', finishMenuFocusHandoff, true)
    return () => {
      cancelAnimationFrame(frame)
      document.removeEventListener('pointerdown', finishMenuFocusHandoff, true)
    }
  }, [])

  function settle(next) {
    if (settledRef.current) return
    settledRef.current = true
    const title = next?.trim()
    if (title && title !== chat.title) onSubmit(title)
    else onCancel()
  }

  return (
    <div className="project-chats__row project-chats__row--editing">
      <MessageSquare width={16} height={16} aria-hidden="true" />
      <input
        ref={inputRef}
        className="project-chats__rename"
        defaultValue={chat.title || ''}
        maxLength={drawerNameMaxLength('chat')}
        aria-label="Rename chat"
        onChange={finishMenuFocusHandoff}
        onKeyDown={event => {
          finishMenuFocusHandoff()
          if (event.nativeEvent?.isComposing) return
          if (event.key === 'Enter') {
            event.preventDefault()
            settle(event.currentTarget.value)
          } else if (event.key === 'Escape') {
            event.preventDefault()
            settle(null)
          }
        }}
        onBlur={() => {
          const input = inputRef.current
          requestAnimationFrame(() => {
            const active = document.activeElement
            if (active === input) return
            // Menu teardown may drop focus to <body>. Recover once, only
            // before the owner interacts; later click-away blurs must settle.
            const focusDropped = !active
              || active === document.body
              || active === document.documentElement
            if (menuFocusPendingRef.current && focusDropped && input?.isConnected && !settledRef.current) {
              finishMenuFocusHandoff()
              input.focus()
              return
            }
            settle(input?.value)
          })
        }}
      />
    </div>
  )
}

export default function ProjectChatList({
  chats,
  pinnedChatIds,
  chatActions,
  onOpenChat,
  onError,
}) {
  const [menu, setMenu] = useState(null)
  const [renamingId, setRenamingId] = useState(null)
  const restoreFocusRef = useRef(null)
  const { open: openMenuHistory, close: closeMenu } = useHistoryDismiss(() => setMenu(null))
  const menuChat = menu
    ? chats.find(chat => String(chat.id) === menu.chatId) || null
    : null

  // A deleted row cannot keep owning the menu.
  useEffect(() => {
    if (menu && !menuChat) closeMenu()
  }, [menu, menuChat, closeMenu])

  function openMenu(chat, point, trigger, { focusFirstAction = false } = {}) {
    openMenuHistory()
    restoreFocusRef.current = trigger
    setMenu({
      chatId: String(chat.id),
      placement: menuPlacement(point, trigger),
      focusFirstAction,
    })
  }

  const menuChatId = menuChat?.id
  const pinned = menuChat ? pinnedChatIds.has(String(menuChatId)) : false
  const archived = !!menuChat?.archived_at
  const label = chatLabel(menuChat)

  return (
    <>
      <div className="project-chats__list">
        {chats.map(chat => (String(chat.id) === renamingId ? (
          <ProjectChatRename
            key={chat.id}
            chat={chat}
            onSubmit={title => {
              setRenamingId(null)
              void chatActions.onRename(chat.id, title)
            }}
            onCancel={() => setRenamingId(null)}
          />
        ) : (
          <ProjectChatRow
            key={chat.id}
            chat={chat}
            onOpen={onOpenChat}
            onOpenMenu={openMenu}
          />
        )))}
      </div>
      <DrawerItemActionMenu
        open={Boolean(menuChat)}
        itemKind="chat"
        itemName={label}
        pinned={pinned}
        archived={archived}
        placement={menu?.placement}
        focusFirstAction={menu?.focusFirstAction === true}
        restoreFocusRef={restoreFocusRef}
        onClose={closeMenu}
        onPin={() => void chatActions.onPin(menuChatId, !pinned)}
        onArchive={() => void chatActions.onSetArchived(menuChatId, !archived)}
        onCopy={() => void writeClipboardText(label).then(copied => {
          if (!copied) onError?.("Couldn't copy the name.")
        })}
        onRename={() => setRenamingId(String(menuChatId))}
        onDelete={() => void chatActions.onDelete(menuChatId)}
      />
    </>
  )
}
