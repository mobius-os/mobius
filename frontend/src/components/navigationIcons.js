// One ChatGPT SDK icon vocabulary and one presentation class for actions that
// exist in both the collapsed desktop rail and expanded drawer. Keeping the
// class on the exported components prevents either surface from quietly
// changing the same glyph's weight or opacity on its own.
import { createElement } from 'react'
import {
  Chat,
  ComposeEditSquare,
  Grid,
  MagnifyingGlassSearch,
  SettingsSlider,
} from '@openai/apps-sdk-ui/components/Icon'
import './navigationIcons.css'

function navigationIcon(Icon) {
  return function NavigationIcon({ className = '', ...props }) {
    return createElement(Icon, {
      ...props,
      className: `navigation-action-icon${className ? ` ${className}` : ''}`,
    })
  }
}

export const ChatNavIcon = navigationIcon(Chat)
export const NewChatNavIcon = navigationIcon(ComposeEditSquare)
export const AppsNavIcon = navigationIcon(Grid)
export const SearchNavIcon = navigationIcon(MagnifyingGlassSearch)
export const SettingsNavIcon = navigationIcon(SettingsSlider)
