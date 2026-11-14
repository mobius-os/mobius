// Keep the registry response envelope so rows, defaults, and replacements
// describe the same service snapshot. Plain maps remain valid cached data.
export function catalogProviders(catalog) {
  return catalog?.providers && !Array.isArray(catalog.providers)
    ? catalog.providers : (catalog || {})
}

export function catalogDefault(catalog, provider) {
  const value = catalog?.defaults?.[provider]
  return typeof value === 'string' && value ? value : ''
}

export function selectedCatalogModel(catalog, provider, savedId) {
  if (typeof savedId !== 'string' || !savedId) return savedId
  const replacement = catalog?.replacements?.[provider]?.[savedId]
  const rows = catalogProviders(catalog)?.[provider]
  return typeof replacement === 'string'
    && Array.isArray(rows)
    && rows.some(row => row?.id === replacement)
    ? replacement : savedId
}
