import { atom } from 'nanostores'

import { translateNow } from '@/i18n'
import {
  copyTextToClipboard,
  isDesktopFsRemoteMode,
  renameDesktopPath,
  revealDesktopPath,
  trashDesktopPath
} from '@/lib/desktop-fs'
import { downloadGatewayMediaFile } from '@/lib/media'
import { $projectFilesystemScope, projectFilesystemIsLocal } from '@/lib/project-filesystem-capability'
import { $gatewayActivationGeneration } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $connection } from '@/store/session'
import { notifyWorkspaceChanged } from '@/store/workspace-events'

// Shared file-row actions for BOTH trees (the file browser + the review/git
// tree): reveal, copy path, download (remote), rename, delete. Rename/delete
// route through a single dialog set (driven by this atom, rendered once by
// `FileActionDialogs`) instead of one dialog per row. After a successful
// mutation we bump the workspace tick so every git-/fs-mirroring surface
// refreshes.

export interface FileActionTarget {
  isDirectory: boolean
  /** Display name (basename) shown in dialogs. */
  name: string
  /** Absolute path on disk. */
  path: string
}

// Delete routes through a single confirm dialog (rendered once). Rename is
// INLINE (VS Code style — an input in the row), driven by `$renamingPath`.
export type FileActionDialog = { kind: 'delete'; context: number; id: number } & FileActionTarget
let dialogSequence = 0

let fileActionGeneration = 0
export const captureFileActionContext = (): number => fileActionGeneration
export const isFileActionContextCurrent = (context: number): boolean => context === fileActionGeneration

export const $fileActionDialog = atom<FileActionDialog | null>(null)

export function requestFileDelete(target: FileActionTarget): void {
  if (projectFilesystemIsLocal()) {
    $fileActionDialog.set({ kind: 'delete', ...target, context: captureFileActionContext(), id: ++dialogSequence })
  }
}

export function closeFileActionDialog(): void {
  $fileActionDialog.set(null)
}

// Absolute path of the row currently being renamed inline, or null. A row whose
// path matches renders an edit input in place of its label; F2 / Enter (on a
// focused row) and the context-menu "Rename" all set this.
export const $renamingPath = atom<null | string>(null)
export const $inlineRenameGeneration = atom(0)

export function captureInlineRenameOwner(): () => boolean {
  const context = captureFileActionContext()
  const generation = $inlineRenameGeneration.get()

  return () => isFileActionContextCurrent(context) && generation === $inlineRenameGeneration.get()
}

export function beginInlineRename(path: string): void {
  if (projectFilesystemIsLocal()) {
    $inlineRenameGeneration.set($inlineRenameGeneration.get() + 1)
    $renamingPath.set(path)
  }
}

export function cancelInlineRename(): void {
  $inlineRenameGeneration.set($inlineRenameGeneration.get() + 1)
  $renamingPath.set(null)
}

function invalidateFileActions(): void {
  fileActionGeneration += 1
  closeFileActionDialog()
  cancelInlineRename()
}

$connection.listen(invalidateFileActions)
$projectFilesystemScope.listen(invalidateFileActions)
$gatewayActivationGeneration.listen(invalidateFileActions)

// ── Direct (no-dialog) actions ───────────────────────────────────────────────

export async function revealFile(path: string): Promise<void> {
  if (!projectFilesystemIsLocal()) {
    return
  }

  try {
    await revealDesktopPath(path)
  } catch (error) {
    notifyError(error, translateNow('errors.genericFailure'))
  }
}

export async function copyFilePath(path: string): Promise<void> {
  try {
    await copyTextToClipboard(path)
    notify({ durationMs: 1500, kind: 'info', message: translateNow('fileMenu.pathCopied') })
  } catch (error) {
    notifyError(error, translateNow('common.copyFailed'))
  }
}

/** Remote Files panel can list gateway files but Reveal/Rename/Delete are local-only.
 *  Download is the local-copy affordance. Folders stay out — `/api/fs/download`
 *  streams a single file. */
export function shouldOfferRemoteFileDownload(isDirectory: boolean, remote = isDesktopFsRemoteMode()): boolean {
  return remote && !isDirectory
}

export async function downloadRemoteFile(path: string): Promise<void> {
  try {
    const result = await downloadGatewayMediaFile(path)

    if (result.canceled || !result.saved) {
      return
    }

    notify({ durationMs: 1500, kind: 'info', message: translateNow('fileMenu.downloadSaved') })
  } catch (error) {
    notifyError(error, translateNow('fileMenu.downloadFailed'))
  }
}

/** Strip a `relativeTo` prefix to produce a repo/cwd-relative path. */
export function toRelativePath(path: string, relativeTo: string): string {
  const base = relativeTo.replace(/[\\/]+$/, '')

  if (path === base) {
    return path
  }

  return path.startsWith(`${base}/`) || path.startsWith(`${base}\\`) ? path.slice(base.length + 1) : path
}

// ── Dialog-confirmed mutations (called by FileActionDialogs) ──────────────────

export async function executeFileRename(
  path: string,
  newName: string,
  context = captureFileActionContext()
): Promise<void> {
  if (!projectFilesystemIsLocal() || context !== fileActionGeneration || $renamingPath.get() !== path) {
    return
  }

  try {
    await renameDesktopPath(path, newName)

    if (isFileActionContextCurrent(context)) {
      notifyWorkspaceChanged()
    }
  } catch (error) {
    if (isFileActionContextCurrent(context)) {
      throw error
    }
  }
}

export async function executeFileDelete(path: string, context = captureFileActionContext()): Promise<void> {
  if (!projectFilesystemIsLocal() || context !== fileActionGeneration || $fileActionDialog.get()?.path !== path) {
    return
  }

  try {
    await trashDesktopPath(path)

    if (isFileActionContextCurrent(context)) {
      notifyWorkspaceChanged()
    }
  } catch (error) {
    if (isFileActionContextCurrent(context)) {
      throw error
    }
  }
}
