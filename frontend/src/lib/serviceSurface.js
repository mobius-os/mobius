/**
 * Give every explicit service-surface open a distinct document navigation.
 * The correlation is a browser routing/cache identity only; it is neither
 * secret nor accepted as authorization by the server.
 */
export function serviceSurfaceFrameUrl(surfaceUrl, correlation) {
  const url = new URL(surfaceUrl)
  url.searchParams.set('mobius_instance', correlation)
  url.hash = correlation
  return url.href
}
