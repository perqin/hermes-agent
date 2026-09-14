import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesRepoStatus } from '@/global'
import { projectFilesystemConfigWritten, setProjectFilesystemScope } from '@/lib/project-filesystem-capability'

import {
  $repoStatus,
  $repoStatusByCwd,
  $repoStatusLoading,
  _resetCodingStatusForTests,
  openWorktreeDialog,
  refreshAllRepoStatuses,
  refreshRepoStatus,
  registerRepoStatusCwd,
  repoChangeKindForPath,
  repoStatusForCwd,
  resolveWorktreeRepoPath
} from './coding-status'
import * as projectsStore from './projects'
import { $currentCwd, $selectedStoredSessionId } from './session'

const sampleStatus: HermesRepoStatus = {
  branch: 'feature/login',
  defaultBranch: 'main',
  detached: false,
  ahead: 1,
  behind: 0,
  staged: 1,
  unstaged: 2,
  untracked: 0,
  conflicted: 0,
  changed: 3,
  added: 12,
  removed: 4,
  files: []
}

const otherStatus: HermesRepoStatus = {
  ...sampleStatus,
  branch: 'bb/other-worktree',
  added: 3,
  removed: 1,
  changed: 1,
  staged: 0,
  unstaged: 1
}

function stubProbe(impl: (cwd: string) => Promise<HermesRepoStatus | null>) {
  ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = { git: { repoStatus: impl } }
}

describe('refreshRepoStatus', () => {
  beforeEach(() => {
    setProjectFilesystemScope('local')
    vi.useFakeTimers()
    _resetCodingStatusForTests()
    $currentCwd.set('')
    $selectedStoredSessionId.set(null)
    // Drain the cwd/session subscribe side-effects the sets above kick off.
    vi.advanceTimersByTime(200)
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    _resetCodingStatusForTests()
  })

  afterEach(() => {
    setProjectFilesystemScope('unknown')
    _resetCodingStatusForTests()
    vi.clearAllTimers()
    vi.useRealTimers()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('populates the per-cwd cache and the primary computed for that cwd', async () => {
    stubProbe(async () => sampleStatus)
    $currentCwd.set('/repo')
    await refreshRepoStatus('/repo')
    expect(repoStatusForCwd('/repo').get()).toEqual(sampleStatus)
    expect($repoStatus.get()).toEqual(sampleStatus)
  })

  it('falls back to the active session cwd when none is passed', async () => {
    const probe = vi.fn(async () => sampleStatus)
    stubProbe(probe)
    $currentCwd.set('/active/repo')
    await refreshRepoStatus()
    expect(probe).toHaveBeenCalledWith('/active/repo')
    expect($repoStatus.get()).toEqual(sampleStatus)
  })

  it('leaves the cache alone (primary reads null) when there is no cwd', async () => {
    stubProbe(async () => sampleStatus)
    $currentCwd.set('/repo')
    await refreshRepoStatus('/repo')
    expect($repoStatus.get()).toEqual(sampleStatus)

    // Blank target is a no-op: other worktrees' cached truth must stay put.
    await refreshRepoStatus('   ')
    expect(repoStatusForCwd('/repo').get()).toEqual(sampleStatus)
    $currentCwd.set('')
    expect($repoStatus.get()).toBeNull()
  })

  it('clears every cached status when the probe is unavailable (remote backend)', async () => {
    $currentCwd.set('/repo')
    $repoStatusByCwd.set({ '/repo': sampleStatus, '/other': otherStatus })
    await refreshRepoStatus('/repo')
    expect($repoStatusByCwd.get()).toEqual({})
    expect($repoStatus.get()).toBeNull()
  })

  it('clears only the failing cwd when the probe throws', async () => {
    stubProbe(async cwd => {
      if (cwd === '/bad') {
        throw new Error('not a repo')
      }

      return sampleStatus
    })
    $currentCwd.set('/bad')
    $repoStatusByCwd.set({ '/bad': otherStatus, '/good': sampleStatus })
    await refreshRepoStatus('/bad')
    expect(repoStatusForCwd('/bad').get()).toBeNull()
    expect(repoStatusForCwd('/good').get()).toEqual(sampleStatus)
    expect($repoStatus.get()).toBeNull()
  })

  it('never publishes an old worktree status onto the primary after the active cwd moves', async () => {
    let resolveOld!: (status: HermesRepoStatus | null) => void
    stubProbe(
      () =>
        new Promise(resolve => {
          resolveOld = resolve
        })
    )

    // Explicit probe (not the debounced cwd edge) so the hang is fully under
    // our control — same race window the coding rail used to hit after a
    // session switch mid-probe.
    const inflight = refreshRepoStatus('/repo-a')
    $currentCwd.set('/repo-a')
    await Promise.resolve()

    $currentCwd.set('/repo-b')
    expect($repoStatus.get()).toBeNull()

    resolveOld(sampleStatus)
    await inflight

    // Primary follows the NEW cwd (empty). The old worktree may still cache the
    // late result under its own key — that's what lets a tile rail paint
    // instantly — but it must not leak onto the main rail via $repoStatus.
    expect($repoStatus.get()).toBeNull()
    expect(repoStatusForCwd('/repo-a').get()).toEqual(sampleStatus)
  })

  it('keeps independent statuses for two live worktrees', async () => {
    stubProbe(async cwd => (cwd === '/repo-a' ? sampleStatus : otherStatus))
    await refreshRepoStatus('/repo-a')
    await refreshRepoStatus('/repo-b')
    expect(repoStatusForCwd('/repo-a').get()).toEqual(sampleStatus)
    expect(repoStatusForCwd('/repo-b').get()).toEqual(otherStatus)

    $currentCwd.set('/repo-a')
    expect($repoStatus.get()).toEqual(sampleStatus)
    $currentCwd.set('/repo-b')
    expect($repoStatus.get()).toEqual(otherStatus)
  })

  it('runs one probe at a time and coalesces overlap into one trailing refresh per drain', async () => {
    const resolvers: Array<(status: HermesRepoStatus | null) => void> = []
    const calls: string[] = []
    let active = 0
    let maxActive = 0

    stubProbe(
      cwd =>
        new Promise(resolve => {
          calls.push(cwd)
          active++
          maxActive = Math.max(maxActive, active)
          resolvers.push(status => {
            active--
            resolve(status)
          })
        })
    )

    $currentCwd.set('/repo-c')
    const first = refreshRepoStatus('/repo-a')
    const second = refreshRepoStatus('/repo-b')
    const third = refreshRepoStatus('/repo-c')

    expect(calls).toEqual(['/repo-a'])
    expect(maxActive).toBe(1)
    expect($repoStatusLoading.get()).toBe(true)

    resolvers.shift()?.(sampleStatus)
    await Promise.resolve()
    await Promise.resolve()

    expect(maxActive).toBe(1)
    expect(calls.length).toBe(2)

    resolvers.shift()?.(otherStatus)
    await Promise.resolve()
    await Promise.resolve()
    resolvers.shift()?.(sampleStatus)
    await Promise.all([first, second, third])

    expect(maxActive).toBe(1)
    expect(calls).toEqual(['/repo-a', '/repo-b', '/repo-c'])
    expect(repoStatusForCwd('/repo-a').get()).toEqual(sampleStatus)
    expect(repoStatusForCwd('/repo-b').get()).toEqual(otherStatus)
    expect(repoStatusForCwd('/repo-c').get()).toEqual(sampleStatus)
    expect($repoStatus.get()).toEqual(sampleStatus)
    expect($repoStatusLoading.get()).toBe(false)
  })

  it('refreshes when the stored session id changes even if the cwd is unchanged', async () => {
    const probe = vi.fn(async () => sampleStatus)
    stubProbe(probe)

    $currentCwd.set('/repo')
    $selectedStoredSessionId.set('session-a')
    // The cwd subscription fires on the set above; drain the debounced refresh.
    vi.advanceTimersByTime(200)
    await vi.runAllTicks()

    probe.mockClear()

    // Switch to a different session in the SAME repo dir. The cwd atom value is
    // identical, so its subscription would not re-fire — but the stored-session
    // id did change, which must still trigger a probe so the branch label
    // tracks the new session's checked-out branch.
    $selectedStoredSessionId.set('session-b')
    vi.advanceTimersByTime(200)
    await vi.runAllTicks()

    expect(probe).toHaveBeenCalledWith('/repo')
  })

  it('registerRepoStatusCwd keeps that worktree in unscoped refreshes', async () => {
    const probe = vi.fn(async cwd => (cwd === '/tile' ? otherStatus : sampleStatus))
    stubProbe(probe)

    $currentCwd.set('/main')
    const release = registerRepoStatusCwd('/tile')
    // Drain the register kick + cwd edge so the assert only covers the
    // unscoped fan-out.
    vi.advanceTimersByTime(200)
    await refreshAllRepoStatuses()
    probe.mockClear()

    // Unscoped fan-out: every registered worktree + primary, not main only.
    await refreshAllRepoStatuses()

    const probed = [...new Set(probe.mock.calls.map(call => call[0]))].sort()
    expect(probed).toEqual(['/main', '/tile'])
    expect(repoStatusForCwd('/tile').get()).toEqual(otherStatus)
    expect(repoStatusForCwd('/main').get()).toEqual(sampleStatus)

    release?.()
  })
})

describe('resolveWorktreeRepoPath', () => {
  it('does not reopen a stale worktree dialog after terminal config invalidates an in-flight resolver', async () => {
    let finish!: (scope: 'local') => void
    const capture = vi.spyOn(projectsStore, 'captureProjectPathContext').mockReturnValue({} as never)

    const scope = vi
      .spyOn(projectsStore, 'projectFilesystemScope')
      .mockReturnValue(new Promise(resolve => (finish = resolve)))

    setProjectFilesystemScope('local')
    stubProbe(vi.fn(async () => sampleStatus))
    projectsStore.$projectScope.set('p-local')
    projectsStore.$projectTree.set([
      { id: 'p-local', label: 'Local', path: '/repo', repos: [], sessionCount: 0 }
    ])

    const pending = openWorktreeDialog()
    projectFilesystemConfigWritten()
    setProjectFilesystemScope('local')
    finish('local')
    await pending

    expect(projectsStore.$worktreeDialog.get()).toBeNull()
    capture.mockRestore()
    scope.mockRestore()
  })

  it('does not send a non-local Project path to the desktop Git transport', async () => {
    const probe = vi.fn(async () => sampleStatus)
    const scope = vi.spyOn(projectsStore, 'projectFilesystemScope').mockResolvedValue('non_local')
    stubProbe(probe)
    projectsStore.$projectScope.set('p-remote')
    projectsStore.$projectTree.set([
      {
        id: 'p-remote',
        label: 'Remote',
        path: '/backend/repo',
        repos: [],
        sessionCount: 0
      }
    ])

    try {
      await expect(resolveWorktreeRepoPath()).resolves.toBe('')
      expect(probe).not.toHaveBeenCalled()
    } finally {
      scope.mockRestore()
      projectsStore.$projectScope.set(projectsStore.ALL_PROJECTS)
      projectsStore.$projectTree.set([])
      delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    }
  })
})

describe('repoChangeKindForPath', () => {
  it('does not notify a row when only another path changes', () => {
    $currentCwd.set('/repo')
    $repoStatusByCwd.set({ '/repo': { ...sampleStatus, files: [] } })
    const row = repoChangeKindForPath('/repo/a.ts')
    const listener = vi.fn()
    const unsubscribe = row.subscribe(listener)

    $repoStatusByCwd.set({
      '/repo': {
        ...sampleStatus,
        files: [{ path: 'b.ts', untracked: true } as HermesRepoStatus['files'][number]]
      }
    })
    expect(listener).toHaveBeenCalledTimes(1)

    $repoStatusByCwd.set({
      '/repo': {
        ...sampleStatus,
        files: [{ path: 'a.ts', untracked: true } as HermesRepoStatus['files'][number]]
      }
    })
    expect(listener).toHaveBeenCalledTimes(2)
    expect(listener.mock.calls.at(-1)?.[0]).toBe('added')

    unsubscribe()
  })
})
