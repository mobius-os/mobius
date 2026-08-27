function appIdentitySets(apps) {
  const ids = new Set()
  const slugs = new Set()
  for (const app of Array.isArray(apps) ? apps : []) {
    const id = Number(app?.id ?? app?.app_id)
    const slug = String(app?.slug || '').trim()
    if (Number.isInteger(id) && id > 0) ids.add(id)
    if (slug) slugs.add(slug)
  }
  return { ids, slugs }
}

export function artifactRelatedToApps(record, apps) {
  const related = Array.isArray(record?.related_apps) ? record.related_apps : []
  if (related.length === 0) return false
  const { ids, slugs } = appIdentitySets(apps)
  if (ids.size === 0 && slugs.size === 0) return false
  return related.some((app) => {
    const id = Number(app?.id ?? app?.app_id)
    const slug = String(app?.slug || '').trim()
    return (Number.isInteger(id) && id > 0 && ids.has(id))
      || (slug && slugs.has(slug))
  })
}
