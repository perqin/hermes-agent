import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { projectFilesystemConfigWritten, setProjectFilesystemScope } from '@/lib/project-filesystem-capability'
import { $fileActionDialog, $renamingPath, beginInlineRename, requestFileDelete } from '@/store/file-actions'
import { $gatewayActivationGeneration } from '@/store/gateway'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $connection } from '@/store/session'
import { $workspaceChangeTick } from '@/store/workspace-events'

import { FileActionDialogs, InlineRenameInput } from './file-actions'

beforeEach(() => {
  setProjectFilesystemScope('local')
  clearNotifications()
})
afterEach(() => {
  cleanup()
  setProjectFilesystemScope('unknown')
  $connection.set(null)
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('inline rename continuation ownership', () => {
  it('allows the replacement same-path editor to submit after a batched roundtrip', async () => {
    let resolve!: (value: { path: string }) => void

    const renamePath = vi.fn(
      () =>
        new Promise<{ path: string }>(yes => {
          resolve = yes
        })
    )

    window.hermesDesktop = { renamePath } as unknown as NonNullable<Window['hermesDesktop']>
    beginInlineRename('/repo/a')
    render(<InlineRenameInput name="a" path="/repo/a" />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'b' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    act(() => {
      projectFilesystemConfigWritten()
      setProjectFilesystemScope('local')
      beginInlineRename('/repo/a')
    })
    await act(async () => resolve({ path: '/repo/b' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'c' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(renamePath).toHaveBeenCalledTimes(2)
    expect(renamePath).toHaveBeenLastCalledWith('/repo/a', 'c')
    await act(async () => resolve({ path: '/repo/c' }))
  })

  it('keeps current delete failures inline and retryable', async () => {
    window.hermesDesktop = {
      trashPath: vi.fn().mockRejectedValue(new Error('delete failed'))
    } as unknown as NonNullable<Window['hermesDesktop']>
    requestFileDelete({ isDirectory: false, name: 'a', path: '/repo/a' })
    render(<FileActionDialogs />)
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Delete' })))
    expect(screen.getByText('delete failed')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false)
    expect($fileActionDialog.get()?.path).toBe('/repo/a')
  })

  it('notifies a current rename failure and closes only its editor', async () => {
    window.hermesDesktop = {
      renamePath: vi.fn().mockRejectedValue(new Error('rename failed'))
    } as unknown as NonNullable<Window['hermesDesktop']>
    beginInlineRename('/repo/a')
    render(<InlineRenameInput name="a" path="/repo/a" />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'b' } })
    await act(async () => fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' }))
    expect($notifications.get().map(n => n.message)).toEqual(['rename failed'])
    expect($renamingPath.get()).toBeNull()
  })

  it.each([false, true].flatMap(reject => ['config', 'activation', 'profile'].map(route => ({ reject, route }))))(
    'keeps a replacement delete dialog untouched ($route, reject=$reject)',
    async ({ reject, route }) => {
      let resolve!: (value: { ok: boolean }) => void
      let fail!: (error: Error) => void
      window.hermesDesktop = {
        trashPath: vi.fn(
          () =>
            new Promise<{ ok: boolean }>((yes, no) => {
              resolve = yes
              fail = no
            })
        )
      } as unknown as NonNullable<Window['hermesDesktop']>
      const target = { isDirectory: false, name: 'a', path: '/repo/a' }
      requestFileDelete(target)
      render(<FileActionDialogs />)
      fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
      act(() => {
        if (route === 'config') {
          projectFilesystemConfigWritten()
        } else if (route === 'activation') {
          $gatewayActivationGeneration.set($gatewayActivationGeneration.get() + 1)
        } else {
          $connection.set({ mode: 'local', profile: 'replacement' } as never)
        }

        setProjectFilesystemScope('local')
        requestFileDelete(target)
      })
      const dialog = $fileActionDialog.get()
      const tick = $workspaceChangeTick.get()
      await act(async () => {
        if (reject) {
          fail(new Error('old delete failed'))
        } else {
          resolve({ ok: true })
        }
      })
      expect($workspaceChangeTick.get()).toBe(tick)
      expect(screen.queryByText('old delete failed')).toBeNull()
      expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false)
      await act(async () => {
        await new Promise(resolve => setTimeout(resolve, 650))
      })
      expect($fileActionDialog.get()).toBe(dialog)
    }
  )

  it('does not cancel a replacement same-path editor in the same route', async () => {
    let resolve!: (value: { path: string }) => void
    window.hermesDesktop = {
      renamePath: vi.fn(
        () =>
          new Promise<{ path: string }>(yes => {
            resolve = yes
          })
      )
    } as unknown as NonNullable<Window['hermesDesktop']>
    beginInlineRename('/repo/a')
    const view = render(<InlineRenameInput name="a" path="/repo/a" />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'b' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    view.unmount()
    beginInlineRename('/repo/a')
    render(<InlineRenameInput name="a" path="/repo/a" />)
    await act(async () => resolve({ path: '/repo/b' }))
    expect($renamingPath.get()).toBe('/repo/a')
  })

  it.each([false, true].flatMap(reject => ['config', 'activation', 'profile'].map(route => ({ reject, route }))))(
    'preserves a new same-path editor after retired completion ($route, reject=$reject)',
    async ({ reject, route }) => {
      let resolve!: (value: { path: string }) => void
      let fail!: (error: Error) => void

      const renamePath = vi.fn(
        () =>
          new Promise<{ path: string }>((yes, no) => {
            resolve = yes
            fail = no
          })
      )

      window.hermesDesktop = { renamePath } as unknown as NonNullable<Window['hermesDesktop']>
      beginInlineRename('/repo/a')
      const view = render(<InlineRenameInput name="a" path="/repo/a" />)
      fireEvent.change(screen.getByRole('textbox'), { target: { value: 'b' } })
      fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
      expect(renamePath).toHaveBeenCalledOnce()
      act(() => {
        if (route === 'config') {
          projectFilesystemConfigWritten()
        } else if (route === 'activation') {
          $gatewayActivationGeneration.set($gatewayActivationGeneration.get() + 1)
        } else {
          $connection.set({ mode: 'local', profile: 'replacement' } as never)
        }

        setProjectFilesystemScope('local')
        beginInlineRename('/repo/a')
      })
      view.unmount()
      render(<InlineRenameInput name="a" path="/repo/a" />)
      const tick = $workspaceChangeTick.get()
      await act(async () => {
        if (reject) {
          fail(new Error('old rename failed'))
        } else {
          resolve({ path: '/repo/b' })
        }
      })
      expect($renamingPath.get()).toBe('/repo/a')
      expect($workspaceChangeTick.get()).toBe(tick)
      expect($notifications.get()).toEqual([])
      expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('a')
    }
  )
})
