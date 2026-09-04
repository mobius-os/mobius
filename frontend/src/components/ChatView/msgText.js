export function stripAugmentation(text) {
  let cleaned = text.replace(/\s*<agent_experience>[\s\S]*?<\/agent_experience>\s*/g, '\n\n')
  // An embedded app (window.mobius.chat getContext) appends a compact
  // <app_state> block to the SENT message so the agent has live app context.
  // It's machinery, not the user's words — hide it from the displayed
  // transcript exactly like <agent_experience>. The persisted content keeps it
  // for the model; only the on-screen render is cleaned.
  cleaned = cleaned.replace(/\s*<app_state>[\s\S]*?<\/app_state>\s*/g, '\n\n')
  // Preserve a paragraph boundary when removing the hidden attachment manifest.
  // Multiple queued messages are joined with a single newline before steering;
  // if an image-bearing message contributes a trailing "Files in this session"
  // block, deleting the block AND all surrounding whitespace glues the next
  // queued message directly onto the previous one. Replace with one newline,
  // then normalize.
  cleaned = cleaned.replace(/(?:\s*\[Files in this session:\n[\s\S]*?\]\s*)+/g, '\n')
  return cleaned.replace(/\n{3,}/g, '\n\n').trim()
}
