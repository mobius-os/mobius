// Elements that act on a press themselves: native controls, and custom
// controls that declare an ARIA widget role (a div with role="radio" is as much
// a control as a button). Surfaces that react to a press on their content, such
// as revealing a reply's details or focusing a pane's composer, leave presses
// on these to the control.
const WIDGET_ROLES = [
  'button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'option',
  'menuitem', 'menuitemcheckbox', 'menuitemradio',
  'slider', 'spinbutton', 'combobox', 'textbox',
]

export const INTERACTIVE_TARGET = [
  'a', 'button', 'input', 'textarea', 'select', 'summary', '[contenteditable="true"]',
  ...WIDGET_ROLES.map(role => `[role="${role}"]`),
].join(',')
