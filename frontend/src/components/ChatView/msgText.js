export function stripAugmentation(text) {
  let cleaned = text.replace(/\s*<agent_experience>[\s\S]*?<\/agent_experience>\s*/g, '\n\n')
  // Preserve a paragraph boundary when removing the hidden attachment manifest.
  // Multiple queued messages are joined with a single newline before steering;
  // if an image-bearing message contributes a trailing "Files in this session"
  // block, deleting the block AND all surrounding whitespace glues the next
  // queued message directly onto the previous one. Replace with one newline,
  // then normalize.
  cleaned = cleaned.replace(/(?:\s*\[Files in this session:\n[\s\S]*?\]\s*)+/g, '\n')
  return cleaned.replace(/\n{3,}/g, '\n\n').trim()
}
