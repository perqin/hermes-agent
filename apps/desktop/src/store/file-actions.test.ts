import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setProjectFilesystemScope } from '@/lib/project-filesystem-capability'
import { $gatewayActivationGeneration } from '@/store/gateway'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $connection } from '@/store/session'

vi.mock('@/lib/media', () => ({
  downloadGatewayMediaFile: vi.fn()
}))

const media = await import('@/lib/media')
const downloadGatewayMediaFile = vi.mocked(media.downloadGatewayMediaFile)

const {
  beginInlineRename,
  downloadRemoteFile,
  executeFileDelete,
  executeFileRename,
  requestFileDelete,
  revealFile,
  shouldOfferRemoteFileDownload,
  $fileActionDialog,
  $renamingPath
} = await import('./file-actions')

describe('project file action locality', () => {
  it('rejects old callbacks even if the next profile requests the same path', async () => {
    const renamePath = vi.fn(async () => ({ path: '/same/b' }))

    const trashPath = vi.fn(async () => ({ ok: true }))

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { renamePath, trashPath }
    setProjectFilesystemScope('local')
    beginInlineRename('/same/a')
    requestFileDelete({ isDirectory: false, name: 'a', path: '/same/a' })
    const origin = $fileActionDialog.get()!.context
    $connection.set({ mode: 'local', profile: 'other' } as never)
    beginInlineRename('/same/a')
    requestFileDelete({ isDirectory: false, name: 'a', path: '/same/a' })
    await executeFileRename('/same/a', 'b', origin)
    await executeFileDelete('/same/a', origin)
    expect(renamePath).not.toHaveBeenCalled()
    expect(trashPath).not.toHaveBeenCalled()
    $connection.set(null)
    setProjectFilesystemScope('unknown')
  })

  it('clears pending paths at the start of gateway activation', () => {
    setProjectFilesystemScope('local')
    beginInlineRename('/old/a')
    requestFileDelete({ isDirectory: false, name: 'a', path: '/old/a' })
    $gatewayActivationGeneration.set($gatewayActivationGeneration.get() + 1)
    expect($renamingPath.get()).toBeNull()
    expect($fileActionDialog.get()).toBeNull()
    setProjectFilesystemScope('unknown')
  })

  it('clears pending actions and refuses stale paths after another local profile activates', async () => {
    const renamePath = vi.fn(async () => ({ path: '/old/b' }))

    const trashPath = vi.fn(async () => ({ ok: true }))

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { renamePath, trashPath }
    setProjectFilesystemScope('local')
    beginInlineRename('/old/a')
    requestFileDelete({ isDirectory: false, name: 'a', path: '/old/a' })
    $connection.set({ mode: 'local', profile: 'other' } as never)
    await executeFileRename('/old/a', 'b')
    await executeFileDelete('/old/a')
    expect(renamePath).not.toHaveBeenCalled()
    expect(trashPath).not.toHaveBeenCalled()
    expect($renamingPath.get()).toBeNull()
    expect($fileActionDialog.get()).toBeNull()
    $connection.set(null)
    setProjectFilesystemScope('unknown')
  })

  it('clears pending actions on capability invalidation before local is restored', async () => {
    const renamePath = vi.fn(async () => ({ path: '/old/b' }))

    const trashPath = vi.fn(async () => ({ ok: true }))

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { renamePath, trashPath }
    setProjectFilesystemScope('local')
    beginInlineRename('/old/a')
    requestFileDelete({ isDirectory: false, name: 'a', path: '/old/a' })
    setProjectFilesystemScope('unknown')
    setProjectFilesystemScope('local')
    await executeFileRename('/old/a', 'b')
    await executeFileDelete('/old/a')
    expect(renamePath).not.toHaveBeenCalled()
    expect(trashPath).not.toHaveBeenCalled()
    expect($renamingPath.get()).toBeNull()
    expect($fileActionDialog.get()).toBeNull()
    setProjectFilesystemScope('unknown')
  })

  it.each(['non_local', 'unknown'] as const)('fails closed before reveal/rename/delete for %s scope', async scope => {
    const revealPath = vi.fn()
    const renamePath = vi.fn()

    const trashPath = vi.fn()

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { renamePath, revealPath, trashPath }
    setProjectFilesystemScope(scope)

    beginInlineRename('/backend/a')
    requestFileDelete({ isDirectory: false, name: 'a', path: '/backend/a' })
    await revealFile('/backend/a')
    await executeFileRename('/backend/a', 'b')
    await executeFileDelete('/backend/a')

    expect($renamingPath.get()).toBeNull()
    expect($fileActionDialog.get()).toBeNull()
    expect(revealPath).not.toHaveBeenCalled()
    expect(renamePath).not.toHaveBeenCalled()
    expect(trashPath).not.toHaveBeenCalled()
    setProjectFilesystemScope('unknown')
  })
})

describe('shouldOfferRemoteFileDownload', () => {
  it('is only for files on a remote backend', () => {
    expect(shouldOfferRemoteFileDownload(false, true)).toBe(true)
    expect(shouldOfferRemoteFileDownload(true, true)).toBe(false)
    expect(shouldOfferRemoteFileDownload(false, false)).toBe(false)
    expect(shouldOfferRemoteFileDownload(true, false)).toBe(false)
  })
})

describe('downloadRemoteFile', () => {
  beforeEach(() => {
    clearNotifications()
    downloadGatewayMediaFile.mockReset()
  })

  afterEach(() => {
    clearNotifications()
  })

  it('saves a remote gateway file through the native download bridge', async () => {
    downloadGatewayMediaFile.mockResolvedValue({ path: '/Users/me/Downloads/notes.md', saved: true })

    await downloadRemoteFile('/home/linux/project/notes.md')

    expect(downloadGatewayMediaFile).toHaveBeenCalledWith('/home/linux/project/notes.md')
    expect($notifications.get()[0]?.message).toBe('Saved')
  })

  it('stays quiet when the save dialog is canceled', async () => {
    downloadGatewayMediaFile.mockResolvedValue({ canceled: true, saved: false })

    await downloadRemoteFile('/home/linux/project/notes.md')

    expect($notifications.get()).toEqual([])
  })

  it('toasts when the gateway download fails', async () => {
    downloadGatewayMediaFile.mockRejectedValue(new Error('Desktop file download bridge is unavailable'))

    await downloadRemoteFile('/home/linux/project/notes.md')

    expect($notifications.get()[0]?.kind).toBe('error')
    expect($notifications.get()[0]?.title).toBe('Download failed')
  })
})
