import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { requestOneShot } from '@/lib/oneshot'
import { setProjectFilesystemScope } from '@/lib/project-filesystem-capability'
import { $notifications, clearNotifications } from '@/store/notifications'
import {
  $reviewCommitMsgBusy,
  $reviewFiles,
  $reviewOpen,
  $reviewScopeCwd,
  $reviewScopeTarget,
  $reviewShipBusy,
  $reviewShipInfo
} from '@/store/review'
import { $currentCwd } from '@/store/session'

import { ReviewShipBar } from './ship-bar'

vi.mock('@/store/coding-status', () => ({ refreshRepoStatus: vi.fn(), repoStatusForCwd: () => ({ get: () => null }) }))
vi.mock('@/lib/oneshot', () => ({ requestOneShot: vi.fn(async () => 'generated') }))
const files = [{ path: 'a.ts', status: 'modified' as const, staged: false, added: 1, removed: 0 }]

beforeEach(() => {
  setProjectFilesystemScope('local')
  $reviewScopeCwd.set(null)
  $reviewScopeTarget.set('main')
  $currentCwd.set('/repo')
  $reviewOpen.set(true)
  $reviewFiles.set(files)
  $reviewShipBusy.set(false)
  $reviewShipInfo.set({ ghReady: false, pr: null })
  clearNotifications()
})
afterEach(() => {
  cleanup()
  setProjectFilesystemScope('unknown')
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('ship bar continuation ownership', () => {
  it.each(['commit', 'generate'] as const)('notifies current %s failures without erasing the draft', async action => {
    window.hermesDesktop = {
      git: {
        review: {
          commit: vi.fn().mockRejectedValue(new Error('current failure')),
          commitContext: vi.fn().mockRejectedValue(new Error('current failure'))
        }
      }
    } as unknown as NonNullable<Window['hermesDesktop']>
    render(<ReviewShipBar />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'keep this draft' } })
    await act(async () => {
      if (action === 'commit') {
        fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', ctrlKey: true })
      } else {
        fireEvent.click(screen.getByRole('button', { name: /generate commit message/i }))
      }
    })
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('keep this draft')
    expect($notifications.get().map(n => n.message)).toEqual(['current failure'])
  })

  it.each([false, true])('reports PR errors only to the current owner (retire=%s)', async retire => {
    let fail!: (error: Error) => void
    window.hermesDesktop = {
      git: {
        review: {
          createPr: vi.fn(
            () =>
              new Promise<{ url: string }>((_, no) => {
                fail = no
              })
          ),
          list: vi.fn(async () => ({ files })),
          shipInfo: vi.fn(async () => ({ ghReady: true, pr: null }))
        }
      }
    } as unknown as NonNullable<Window['hermesDesktop']>
    $reviewShipInfo.set({ ghReady: true, pr: null })
    render(<ReviewShipBar />)
    fireEvent.click(screen.getByRole('button', { name: 'Create PR' }))

    const unlisten = $reviewShipBusy.listen(busy => {
      if (!busy && retire) {
        $reviewScopeTarget.set('other')
      }
    })

    await act(async () => fail(new Error('PR failed')))
    unlisten()
    expect($notifications.get().map(n => n.message)).toEqual(retire ? [] : ['PR failed'])
  })

  it.each([false, true])('guards generation caller after store finalization (reject=%s)', async reject => {
    let resolve!: (text: string) => void
    let fail!: (error: Error) => void
    vi.mocked(requestOneShot).mockImplementationOnce(
      () =>
        new Promise<string>((yes, no) => {
          resolve = yes
          fail = no
        })
    )
    window.hermesDesktop = {
      git: {
        review: {
          commitContext: vi.fn(async () => ({ diff: 'diff', recent: '' })),
          list: vi.fn(async () => ({ files })),
          shipInfo: vi.fn(async () => ({ ghReady: false, pr: null }))
        }
      }
    } as unknown as NonNullable<Window['hermesDesktop']>
    render(<ReviewShipBar />)
    await act(async () => fireEvent.click(screen.getByRole('button', { name: /generate commit message/i })))

    const unlisten = $reviewCommitMsgBusy.listen(busy => {
      if (!busy) {
        $reviewScopeTarget.set('other')
        $reviewScopeTarget.set('main')
        $reviewFiles.set(files)
      }
    })

    await act(async () => {
      if (reject) {
        fail(new Error('retired generation failed'))
      } else {
        resolve('retired generated draft')
      }
    })
    unlisten()
    act(() => $reviewFiles.set(files))
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
    expect($notifications.get()).toEqual([])
  })

  it.each([false, true])('preserves the replacement owner draft (reject=%s)', async reject => {
    let resolve!: () => void
    let fail!: (error: Error) => void

    const commit = vi.fn(
      () =>
        new Promise<void>((yes, no) => {
          resolve = yes
          fail = no
        })
    )

    window.hermesDesktop = {
      git: {
        review: {
          commit,
          list: vi.fn(async () => ({ files })),
          shipInfo: vi.fn(async () => ({ ghReady: false, pr: null }))
        }
      }
    } as unknown as NonNullable<Window['hermesDesktop']>
    render(<ReviewShipBar />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'old draft' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', ctrlKey: true })
    expect(commit).toHaveBeenCalledOnce()
    act(() => {
      $reviewScopeTarget.set('other')
      $reviewScopeTarget.set('main')
      $reviewFiles.set(files)
    })
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'new draft' } })
    await act(async () => {
      if (reject) {
        fail(new Error('retired commit failed'))
      } else {
        resolve()
      }
    })
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('new draft')
    expect($notifications.get()).toEqual([])
  })
})
