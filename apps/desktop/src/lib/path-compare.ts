/** Comparing two paths for identity or containment, across host spellings.
 *
 *  A cwd reaches us from the backend, a picker, a session row and a project
 *  record, and the four don't agree on separator, trailing slash or drive-letter
 *  case. Compare through these rather than `===` / `startsWith`.
 */

/** Path-style-aware spelling: normalize Windows separators while preserving valid POSIX characters. */
export const cleanPath = (path: string): string => {
  const raw = path.trim() ? path : ''

  if (!raw) {
    return ''
  }

  const windows = /^[A-Za-z]:[\\/]/.test(raw) || raw.startsWith('\\\\')
  const normalized = windows ? raw.replace(/\\/g, '/') : raw

  return normalized.replace(/\/+$/, '') || '/'
}

/** Append a child without interpreting or trimming backend-owned path bytes. */
export const joinPath = (parent: string, child: string): string => {
  const windows = /^[A-Za-z]:[\\/]/.test(parent) || parent.startsWith('\\\\')
  const separator = windows && parent.includes('\\') ? '\\' : '/'

  return `${parent}${parent.endsWith('/') || (windows && parent.endsWith('\\')) ? '' : separator}${child}`
}

/** Case-folded comparison key. Windows drive/UNC paths are case-insensitive;
 *  POSIX paths are not, and callers that display a path want its real spelling,
 *  so fold only the key. Expects an already-`cleanPath`ed value. */
export const comparisonPath = (path: string): string =>
  /^[A-Za-z]:(?:\/|$)/.test(path) || path.startsWith('//') ? path.toLowerCase() : path

/** True when `child` IS `parent` or lives underneath it. */
export const isUnderPath = (parent: string, child: string): boolean => {
  const p = comparisonPath(cleanPath(parent))
  const c = comparisonPath(cleanPath(child))

  return c === p || c.startsWith(`${p}/`)
}
