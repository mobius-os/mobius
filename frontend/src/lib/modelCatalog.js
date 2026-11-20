// Keep the registry response envelope. Plain maps remain valid cached data.
export function catalogProviders(catalog) {
  return catalog?.providers && !Array.isArray(catalog.providers)
    ? catalog.providers : (catalog || {})
}

export function catalogDefault(catalog, provider) {
  const value = catalog?.defaults?.[provider]
  return typeof value === 'string' && value ? value : ''
}

export function selectedCatalogModel(catalog, provider, savedId) {
  // Historical selections stay visible as unavailable, never as another model.
  return savedId
}
