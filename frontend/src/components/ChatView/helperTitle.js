/* The one name the owner reads for a helper. The server owns its readable
   fallback (delegations.helper_chat_title); this only guards a missing field. */
export function helperTitle(helper) {
  const title = typeof helper?.title === 'string' ? helper.title.trim() : ''
  return title || helper?.task_key || 'Helper'
}
