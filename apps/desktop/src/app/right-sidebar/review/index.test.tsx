import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setProjectFilesystemScope } from '@/lib/project-filesystem-capability'
import { $notifications, clearNotifications } from '@/store/notifications'
import {
  $reviewFiles,
  $reviewLoading,
  $reviewOpen,
  $reviewRevertTarget,
  $reviewScopeCwd,
  $reviewScopeTarget,
  requestRevert
} from '@/store/review'
import { $currentCwd } from '@/store/session'

import { ReviewPane } from './index'

vi.mock('./file-tree', () => ({ ReviewFileTree: () => null }))
vi.mock('./ship-bar', () => ({ ReviewShipBar: () => null }))
vi.mock('../index', () => ({
  RightSidebarSectionHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  PaneEmptyState: () => null
}))
vi.mock('@/store/coding-status', () => ({ refreshRepoStatus: vi.fn(), repoStatusForCwd: () => ({ get: () => null }) }))

beforeEach(() => {
  setProjectFilesystemScope('local')
  $reviewScopeCwd.set(null)
  $reviewScopeTarget.set('main')
  $currentCwd.set('/repo')
  $reviewOpen.set(true)
  $reviewLoading.set(false)
  $reviewFiles.set([])
  clearNotifications()
})
afterEach(() => {
  cleanup()
  setProjectFilesystemScope('unknown')
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('revert dialog caller ownership', () => {
  it.each([false, true])('notifies stage errors only while caller still owns the pane (retire=%s)', async retire => {
    let fail!: (error: Error) => void
    window.hermesDesktop = {
      git: {
        review: {
          stage: vi.fn(
            () =>
              new Promise<void>((_, no) => {
                fail = no
              })
          ),
          list: vi.fn(async () => ({ files: [] })),
          shipInfo: vi.fn(async () => ({ ghReady: false, pr: null }))
        }
      }
    } as unknown as NonNullable<Window['hermesDesktop']>
    $reviewFiles.set([{ path: 'a.ts', status: '?', staged: false, added: 1, removed: 0 }])
    render(<ReviewPane />)
    fireEvent.click(screen.getByRole('button', { name: 'Stage all' }))
    await act(async () => {
      fail(new Error('stage failed'))
      await Promise.resolve()

      if (retire) {
        $reviewScopeTarget.set('other')
      }
    })
    expect($notifications.get().map(n => n.message)).toEqual(retire ? [] : ['stage failed'])
  })

  it.each([false, true])('does not dismiss a newer same-path confirmation (reject=%s)', async reject => {
    let resolve!: () => void
    let fail!: (error: Error) => void
    window.hermesDesktop = {
      git: {
        review: {
          revert: vi.fn(
            () =>
              new Promise<void>((yes, no) => {
                resolve = yes
                fail = no
              })
          ),
          list: vi.fn(async () => ({ files: [] })),
          shipInfo: vi.fn(async () => ({ ghReady: false, pr: null }))
        }
      }
    } as unknown as NonNullable<Window['hermesDesktop']>
    requestRevert('a.ts')
    render(<ReviewPane />)
    fireEvent.click(screen.getByRole('button', { name: 'Revert' }))
    act(() => {
      $reviewScopeTarget.set('other')
      $reviewScopeTarget.set('main')
      requestRevert('a.ts')
    })
    const target = $reviewRevertTarget.get()
    await act(async () => {
      if (reject) {
        fail(new Error('retired revert failed'))
      } else {
        resolve()
      }
    })
    expect($reviewRevertTarget.get()).toBe(target)
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect($notifications.get()).toEqual([])
  })
})
