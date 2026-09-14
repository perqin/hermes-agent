import { atom } from 'nanostores'

import type { ProjectFilesystemScope } from '@/types/hermes'

/** Conservative renderer snapshot used by synchronous filesystem/Git surfaces. */
export const $projectFilesystemScope = atom<ProjectFilesystemScope>('unknown')

type InvalidatedListener = () => void
const invalidatedListeners = new Set<InvalidatedListener>()
let configGeneration = 0

export function setProjectFilesystemScope(scope: ProjectFilesystemScope): void {
  $projectFilesystemScope.set(scope)
}

export const projectFilesystemIsLocal = (): boolean => $projectFilesystemScope.get() === 'local'
export const projectFilesystemConfigGeneration = (): number => configGeneration

/** Called only after a config write has succeeded. Whole-record writes may replace terminal.backend. */
export function projectFilesystemConfigWritten(): void {
  configGeneration += 1
  setProjectFilesystemScope('unknown')

  for (const listener of invalidatedListeners) {
    listener()
  }
}

export function onProjectFilesystemCapabilityInvalidated(listener: InvalidatedListener): () => void {
  invalidatedListeners.add(listener)

  return () => invalidatedListeners.delete(listener)
}
