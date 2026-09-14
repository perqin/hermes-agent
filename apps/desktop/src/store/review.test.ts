import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesReviewFile, HermesReviewShipInfo } from '@/global'
import { projectFilesystemConfigWritten, setProjectFilesystemScope } from '@/lib/project-filesystem-capability'
import { $gatewayActivationGeneration } from '@/store/gateway'

import { refreshRepoStatus } from './coding-status'
import { stampSessionPrBranch } from './pull-requests'
import {
  $reviewCommitDefault,
  $reviewCommitMsgBusy,
  $reviewDiff,
  $reviewDiffLoading,
  $reviewFiles,
  $reviewIsRepo,
  $reviewLoading,
  $reviewMaxChurn,
  $reviewOpen,
  $reviewRevertTarget,
  $reviewScopeCwd,
  $reviewScopeTarget,
  $reviewSelectedPath,
  $reviewShipBusy,
  $reviewShipInfo,
  $reviewTreeMode,
  cancelRevert,
  clearReviewSelection,
  closeReview,
  commitChanges,
  confirmRevert,
  createOrOpenPr,
  generateCommitMessage,
  openReview,
  openReviewForPath,
  pushChanges,
  refreshReview,
  refreshShipInfo,
  requestRevert,
  revealReview,
  revertReviewFile,
  selectReviewFile,
  stageReviewFile,
  toggleReview,
  toggleReviewTreeMode,
  unstageReviewFile
} from './review'
import { $connection, $currentCwd, $selectedStoredSessionId, $sessions } from './session'

// requestOneShot is the only cross-module dependency that must be faked (it
// reaches the gateway); everything else routes through window.hermesDesktop.git,
// which we stub per-test like the sibling coding-status.test.ts does.
const requestOneShot = vi.fn(async (_args: unknown) => 'generated message')
vi.mock('@/lib/oneshot', () => ({ requestOneShot: (args: unknown) => requestOneShot(args) }))
// refreshRepoStatus is a fire-and-forget side effect of mutations; stub it so it
// doesn't try to hit the (absent) probe and log. repoStatusForCwd is read when a
// new PR binds its session to the branch it came from — no probe here, so no
// branch either.
vi.mock('./coding-status', () => ({
  refreshRepoStatus: vi.fn(),
  repoStatusForCwd: () => ({ get: () => ({ branch: 'origin-branch' }) })
}))
vi.mock('./pull-requests', () => ({ stampSessionPrBranch: vi.fn() }))

function file(path: string, over: Partial<HermesReviewFile> = {}): HermesReviewFile {
  return { path, status: 'modified', staged: false, added: 1, removed: 0, ...over } as HermesReviewFile
}

type ReviewStub = Record<string, ReturnType<typeof vi.fn>>

// Install a review bridge on window.hermesDesktop. Any op not supplied defaults
// to a resolved no-op so a test only declares what it exercises.
function stubReview(over: ReviewStub = {}) {
  const review: ReviewStub = {
    list: vi.fn(async () => ({ files: [] })),
    diff: vi.fn(async () => ''),
    stage: vi.fn(async () => undefined),
    unstage: vi.fn(async () => undefined),
    revert: vi.fn(async () => undefined),
    commit: vi.fn(async () => undefined),
    commitContext: vi.fn(async () => ({ diff: 'd', recent: 'r' })),
    push: vi.fn(async () => undefined),
    shipInfo: vi.fn(async () => ({ ghReady: false, pr: null })),
    createPr: vi.fn(async () => ({ url: 'https://example.com/pr/1' })),
    ...over
  }

  ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = {
    git: { review },
    openExternal: vi.fn()
  }

  return review
}

beforeEach(() => {
  $currentCwd.set('')
  setProjectFilesystemScope('local')
  requestOneShot.mockClear()
  requestOneShot.mockResolvedValue('generated message')
  // Reset stores touched across tests.
  $reviewOpen.set(false)
  $reviewFiles.set([])
  $reviewLoading.set(false)
  $reviewIsRepo.set(true)
  $reviewDiff.set(null)
  $reviewDiffLoading.set(false)
  $reviewSelectedPath.set(null)
  $reviewShipInfo.set({ ghReady: false, pr: null })
  $reviewShipBusy.set(false)
  $reviewCommitMsgBusy.set(false)
  $reviewRevertTarget.set(undefined)
  $reviewScopeCwd.set(null)
  $reviewScopeTarget.set('main')
  $currentCwd.set('/repo')
})

afterEach(() => {
  setProjectFilesystemScope('unknown')
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('retired review errors', () => {
  it.each(['close', 'pinned session'] as const)(
    'retires generation and frees its busy flag on %s',
    async transition => {
      let reject!: (error: Error) => void
      stubReview({
        commitContext: vi.fn(
          () =>
            new Promise((_, fail) => {
              reject = fail
            })
        )
      })

      if (transition === 'pinned session') {
        openReview('/pinned', 'tile')
      }

      const result = generateCommitMessage()

      if (transition === 'close') {
        closeReview()
      } else {
        $selectedStoredSessionId.set('generation-replacement')
      }

      expect($reviewCommitMsgBusy.get()).toBe(false)
      reject(new Error('retired'))
      await expect(result).resolves.toBe('')
    }
  )

  const actions = [
    ['stage', () => stageReviewFile('a.ts')],
    ['unstage', () => unstageReviewFile('a.ts')],
    ['revert', () => revertReviewFile('a.ts')],
    ['commit', () => commitChanges('message')],
    ['push', () => pushChanges()],
    ['createPr', () => createOrOpenPr()],
    ['commitContext', () => generateCommitMessage()]
  ] as const

  it.each(actions)('suppresses retired %s rejection but preserves current errors', async (method, action) => {
    let reject!: (error: Error) => void
    const failure = new Error('operation failed')
    stubReview({
      [method]: vi.fn(
        () =>
          new Promise((_, fail) => {
            reject = fail
          })
      )
    })
    const result = action()
    $reviewScopeTarget.set('other')
    $reviewScopeTarget.set('main')
    reject(failure)
    await expect(result).resolves.toBe(method === 'commitContext' ? '' : undefined)
    stubReview({ [method]: vi.fn().mockRejectedValue(failure) })
    await expect(action()).rejects.toBe(failure)
  })
})

describe('revert confirmation ownership', () => {
  it.each(['close', 'pinned session'] as const)('retires confirmations on %s', async transition => {
    const review = stubReview()

    if (transition === 'pinned session') {
      openReview('/pinned', 'tile')
    }

    requestRevert(null)

    if (transition === 'close') {
      closeReview()
    } else {
      $selectedStoredSessionId.set('replacement-session')
    }

    expect($reviewRevertTarget.get()).toBeUndefined()
    await confirmRevert()
    expect(review.revert).not.toHaveBeenCalled()
  })

  it('does not select a same-path file in a replacement pane after refresh', async () => {
    let finish!: (value: { files: HermesReviewFile[] }) => void

    const pending = new Promise<{ files: HermesReviewFile[] }>(resolve => {
      finish = resolve
    })

    const review = stubReview({ list: vi.fn(() => pending) })
    $reviewOpen.set(true)
    const opening = openReviewForPath('a.ts')
    $reviewScopeTarget.set('other')
    $reviewScopeTarget.set('main')
    $reviewFiles.set([file('a.ts')])
    finish({ files: [file('a.ts')] })
    await opening
    expect($reviewSelectedPath.get()).toBeNull()
    expect(review.diff).not.toHaveBeenCalled()
  })

  it.each(['a.ts', null])('retires pending %s across cwd roundtrip', async path => {
    const review = stubReview()
    requestRevert(path)
    $currentCwd.set('/other')
    $currentCwd.set('/repo')
    expect($reviewRevertTarget.get()).toBeUndefined()
    await confirmRevert()
    expect(review.revert).not.toHaveBeenCalled()
  })
})

describe('refreshReview', () => {
  it('invalidates the pinned repo before gateway activation publishes its connection', async () => {
    const review = stubReview()
    openReview('/old-pinned')
    requestRevert('a.ts')
    $gatewayActivationGeneration.set($gatewayActivationGeneration.get() + 1)
    await stageReviewFile('a.ts')
    await confirmRevert()
    expect(review.stage).not.toHaveBeenCalled()
    expect(review.revert).not.toHaveBeenCalled()
    expect($reviewScopeCwd.get()).toBeNull()
  })

  it('drops pinned review and pending revert when the active profile changes', async () => {
    const review = stubReview()
    openReview('/old-pinned')
    requestRevert('a.ts')
    $connection.set({ mode: 'local', profile: 'other' } as never)
    await stageReviewFile('a.ts')
    await unstageReviewFile('a.ts')
    await confirmRevert()
    expect(review.stage).not.toHaveBeenCalled()
    expect(review.unstage).not.toHaveBeenCalled()
    expect(review.revert).not.toHaveBeenCalled()
    expect($reviewScopeCwd.get()).toBeNull()
    expect($reviewRevertTarget.get()).toBeUndefined()
    expect($reviewFiles.get()).toEqual([])
    $connection.set(null)
  })

  it('is a no-op that clears state when the pane is closed', async () => {
    const review = stubReview()
    $reviewOpen.set(false)
    $reviewFiles.set([file('a.ts')])

    await refreshReview()

    expect(review.list).not.toHaveBeenCalled()
    expect($reviewFiles.get()).toEqual([])
    expect($reviewLoading.get()).toBe(false)
  })

  it('flags not-a-repo (and clears loading) when there is no bridge/cwd', async () => {
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    $reviewOpen.set(true)
    $reviewLoading.set(true)

    await refreshReview()

    expect($reviewIsRepo.get()).toBe(false)
    expect($reviewLoading.get()).toBe(false)
  })

  it('populates the changed-file list from the bridge', async () => {
    stubReview({ list: vi.fn(async () => ({ files: [file('a.ts'), file('b.ts')] })) })
    $reviewOpen.set(true)

    await refreshReview()

    expect($reviewFiles.get().map(f => f.path)).toEqual(['a.ts', 'b.ts'])
    expect($reviewIsRepo.get()).toBe(true)
    expect($reviewLoading.get()).toBe(false)
  })

  it('filters excluded paths (node_modules et al.) out of the list', async () => {
    stubReview({ list: vi.fn(async () => ({ files: [file('src/a.ts'), file('node_modules/x/index.js')] })) })
    $reviewOpen.set(true)

    await refreshReview()

    expect($reviewFiles.get().map(f => f.path)).toEqual(['src/a.ts'])
  })

  it('drops a selection whose file vanished from the new list', async () => {
    stubReview({ list: vi.fn(async () => ({ files: [file('kept.ts')] })) })
    $reviewOpen.set(true)
    $reviewSelectedPath.set('gone.ts')
    $reviewDiff.set('old diff')

    await refreshReview()

    expect($reviewSelectedPath.get()).toBeNull()
    expect($reviewDiff.get()).toBeNull()
  })

  it('clears the list but keeps isRepo true when the bridge throws', async () => {
    stubReview({
      list: vi.fn(async () => {
        throw new Error('git failed')
      })
    })
    $reviewOpen.set(true)
    $reviewFiles.set([file('stale.ts')])

    await refreshReview()

    expect($reviewFiles.get()).toEqual([])
    expect($reviewIsRepo.get()).toBe(true)
    expect($reviewLoading.get()).toBe(false)
  })
})

describe('$reviewMaxChurn', () => {
  it('is the largest added+removed across files', () => {
    $reviewFiles.set([file('a', { added: 3, removed: 2 }), file('b', { added: 10, removed: 1 }), file('c')])
    expect($reviewMaxChurn.get()).toBe(11)
  })

  it('is 0 for an empty list', () => {
    $reviewFiles.set([])
    expect($reviewMaxChurn.get()).toBe(0)
  })
})

describe('selectReviewFile / clearReviewSelection', () => {
  it.each(['resolve', 'reject'])('discards diff %s and finally after config invalidation', async outcome => {
    let resolve!: (value: string) => void
    let reject!: (error: Error) => void
    stubReview({
      diff: vi.fn(
        () =>
          new Promise<string>((done, fail) => {
            resolve = done
            reject = fail
          })
      )
    })
    const pending = selectReviewFile(file('a.ts'))
    projectFilesystemConfigWritten()
    setProjectFilesystemScope('local')
    $reviewSelectedPath.set('a.ts')
    $reviewDiff.set('current diff')
    $reviewDiffLoading.set(true)

    if (outcome === 'resolve') {
      resolve('stale diff')
    } else {
      reject(new Error('stale failure'))
    }

    await pending
    expect($reviewDiff.get()).toBe('current diff')
    expect($reviewDiffLoading.get()).toBe(true)
  })
  it.each(['resolve', 'reject'])('ignores an old-profile diff %s for the same relative path', async outcome => {
    let resolveOld!: (value: string) => void
    let rejectOld!: (error: Error) => void
    let resolveNew!: (value: string) => void
    stubReview({
      diff: vi.fn(
        () =>
          new Promise<string>((resolve, reject) => {
            resolveOld = resolve
            rejectOld = reject
          })
      )
    })
    const oldRequest = selectReviewFile(file('a.ts'))
    $gatewayActivationGeneration.set($gatewayActivationGeneration.get() + 1)
    stubReview({
      diff: vi.fn(
        () =>
          new Promise<string>(resolve => {
            resolveNew = resolve
          })
      )
    })
    openReview('/repo')
    await Promise.resolve()
    const newRequest = selectReviewFile(file('a.ts'))
    $reviewDiff.set('profile B')

    if (outcome === 'resolve') {
      resolveOld('profile A')
    } else {
      rejectOld(new Error('old failure'))
    }

    await oldRequest
    expect($reviewDiff.get()).toBe('profile B')
    expect($reviewDiffLoading.get()).toBe(true)
    resolveNew('new diff')
    await newRequest
    expect($reviewDiff.get()).toBe('new diff')
    expect($reviewDiffLoading.get()).toBe(false)
  })

  it('sets the selected path and fetches its diff', async () => {
    const review = stubReview({ diff: vi.fn(async () => 'the diff') })

    await selectReviewFile(file('a.ts'))

    expect($reviewSelectedPath.get()).toBe('a.ts')
    expect($reviewDiff.get()).toBe('the diff')
    expect($reviewDiffLoading.get()).toBe(false)
    expect(review.diff).toHaveBeenCalledWith('/repo', 'a.ts', 'uncommitted', null, false)
  })

  it('coerces a falsy diff to empty string (not null)', async () => {
    stubReview({ diff: vi.fn(async () => '') })

    await selectReviewFile(file('a.ts'))

    expect($reviewDiff.get()).toBe('')
  })

  it('sets diff null when there is no bridge', async () => {
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop

    await selectReviewFile(file('a.ts'))

    expect($reviewSelectedPath.get()).toBe('a.ts')
    expect($reviewDiff.get()).toBeNull()
  })

  it('clears path, diff and loading', () => {
    $reviewSelectedPath.set('a.ts')
    $reviewDiff.set('x')
    $reviewDiffLoading.set(true)

    clearReviewSelection()

    expect($reviewSelectedPath.get()).toBeNull()
    expect($reviewDiff.get()).toBeNull()
    expect($reviewDiffLoading.get()).toBe(false)
  })
})

describe('view state', () => {
  it('toggleReviewTreeMode flips list <-> tree', () => {
    $reviewTreeMode.set('tree')
    toggleReviewTreeMode()
    expect($reviewTreeMode.get()).toBe('list')
    toggleReviewTreeMode()
    expect($reviewTreeMode.get()).toBe('tree')
  })

  it('openReview opens the pane and kicks off a refresh', async () => {
    const review = stubReview()
    openReview()
    expect($reviewOpen.get()).toBe(true)
    expect($reviewScopeCwd.get()).toBeNull()
    // openReview fires refreshReview + refreshShipInfo without awaiting.
    await Promise.resolve()
    await Promise.resolve()
    expect(review.list).toHaveBeenCalledWith('/repo', 'uncommitted', null)
  })

  it('openReview pins the pane to a tile worktree when scoped', async () => {
    const review = stubReview({
      list: vi.fn(async () => ({ files: [file('tile.ts')] }))
    })

    openReview('/tile-worktree')
    expect($reviewOpen.get()).toBe(true)
    expect($reviewScopeCwd.get()).toBe('/tile-worktree')
    await Promise.resolve()
    await Promise.resolve()
    expect(review.list).toHaveBeenCalledWith('/tile-worktree', 'uncommitted', null)
  })

  it('openReview remembers the tile composer that owns the scoped worktree', () => {
    stubReview()

    openReview('/tile-worktree', 'tile:project-b')

    expect($reviewScopeCwd.get()).toBe('/tile-worktree')
    expect($reviewScopeTarget.get()).toBe('tile:project-b')
  })

  it('revealReview re-homes the origin when the repo stays the same', () => {
    stubReview()
    openReview('/tile-worktree', 'tile:project-a')

    revealReview('/tile-worktree', 'tile:project-b')

    expect($reviewScopeTarget.get()).toBe('tile:project-b')
  })

  it('narrow toggle re-homes the origin before showing the overlay', () => {
    const originalMatchMedia = window.matchMedia

    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn(() => ({ matches: true }))
    })

    try {
      stubReview()
      openReview('/project-a', 'tile:project-a')

      toggleReview('/project-b', 'tile:project-b')

      expect($reviewScopeCwd.get()).toBe('/project-b')
      expect($reviewScopeTarget.get()).toBe('tile:project-b')
    } finally {
      Object.defineProperty(window, 'matchMedia', { configurable: true, value: originalMatchMedia })
    }
  })

  it('closeReview closes the pane, clears selection, and drops scope', () => {
    stubReview()
    $reviewOpen.set(true)
    $reviewScopeCwd.set('/tile-worktree')
    $reviewSelectedPath.set('a.ts')
    $reviewDiff.set('x')

    closeReview()

    expect($reviewOpen.get()).toBe(false)
    expect($reviewScopeCwd.get()).toBeNull()
    expect($reviewScopeTarget.get()).toBe('main')
    expect($reviewSelectedPath.get()).toBeNull()
    expect($reviewDiff.get()).toBeNull()
  })

  it('keeps a pinned diff visible when the main session changes', async () => {
    stubReview({ list: vi.fn(async () => ({ files: [file('tile.ts')] })), diff: vi.fn(async () => 'tile diff') })
    openReview('/tile')
    await Promise.resolve()
    await selectReviewFile(file('tile.ts'))
    $selectedStoredSessionId.set('another-main-session')
    expect($reviewScopeCwd.get()).toBe('/tile')
    expect($reviewFiles.get()).toEqual([file('tile.ts')])
    expect($reviewDiff.get()).toBe('tile diff')
  })

  it('scoped pane ignores main-pane cwd changes', async () => {
    const review = stubReview({
      list: vi.fn(async (cwd: string) => ({ files: [file(cwd === '/tile' ? 'tile.ts' : 'main.ts')] }))
    })

    openReview('/tile')
    await Promise.resolve()
    await Promise.resolve()
    review.list.mockClear()

    // Main session hops repos; the pane is still pinned to the tile.
    $currentCwd.set('/somewhere-else')
    await Promise.resolve()
    await Promise.resolve()

    expect($reviewScopeCwd.get()).toBe('/tile')
    expect(review.list).not.toHaveBeenCalled()
  })
})

describe('mutations', () => {
  it.each(['stage', 'unstage', 'revert', 'commit', 'push'] as const)(
    'does not refresh a new profile after pending %s completes',
    async operation => {
      let resolve!: () => void

      const review = stubReview({
        [operation]: vi.fn(
          () =>
            new Promise<void>(done => {
              resolve = done
            })
        )
      })

      const actions = {
        stage: () => stageReviewFile('a.ts'),
        unstage: () => unstageReviewFile('a.ts'),
        revert: () => revertReviewFile('a.ts'),
        commit: () => commitChanges('message'),
        push: () => pushChanges()
      }

      const pending = actions[operation]()
      $gatewayActivationGeneration.set($gatewayActivationGeneration.get() + 1)
      openReview('/repo')
      await Promise.resolve()
      review.list.mockClear()
      review.shipInfo.mockClear()
      resolve()
      await pending
      expect(review.list).not.toHaveBeenCalled()
      expect(review.shipInfo).not.toHaveBeenCalled()
    }
  )
  it('stageReviewFile forwards the path and re-syncs', async () => {
    const review = stubReview()
    $reviewOpen.set(true) // afterMutation's refreshReview only lists when the pane is open
    await stageReviewFile('a.ts')
    expect(review.stage).toHaveBeenCalledWith('/repo', 'a.ts')
    expect(review.list).toHaveBeenCalled()
  })

  it('unstageReviewFile forwards the path', async () => {
    const review = stubReview()
    await unstageReviewFile('a.ts')
    expect(review.unstage).toHaveBeenCalledWith('/repo', 'a.ts')
  })

  it('revertReviewFile forwards the path', async () => {
    const review = stubReview()
    await revertReviewFile('a.ts')
    expect(review.revert).toHaveBeenCalledWith('/repo', 'a.ts')
  })

  it('stage with null path means "all"', async () => {
    const review = stubReview()
    await stageReviewFile(null)
    expect(review.stage).toHaveBeenCalledWith('/repo', null)
  })
})

describe('revert confirm dialog', () => {
  it('requestRevert opens a target, cancelRevert closes it', () => {
    requestRevert('a.ts')
    expect($reviewRevertTarget.get()).toEqual({ path: 'a.ts' })
    cancelRevert()
    expect($reviewRevertTarget.get()).toBeUndefined()
  })

  it('requestRevert(null) encodes the "revert all" target distinctly from closed', () => {
    requestRevert(null)
    expect($reviewRevertTarget.get()).toEqual({ path: null })
  })

  it('confirmRevert closes the dialog then performs the revert', async () => {
    const review = stubReview()
    requestRevert('a.ts')

    await confirmRevert()

    expect($reviewRevertTarget.get()).toBeUndefined()
    expect(review.revert).toHaveBeenCalledWith('/repo', 'a.ts')
  })

  it('confirmRevert is a no-op when nothing is pending', async () => {
    const review = stubReview()
    $reviewRevertTarget.set(undefined)

    await confirmRevert()

    expect(review.revert).not.toHaveBeenCalled()
  })
})

describe('ship flow', () => {
  it('discards createPr continuation after crossing profiles', async () => {
    let resolve!: (value: { url: string }) => void

    const review = stubReview({
      createPr: vi.fn(
        () =>
          new Promise<{ url: string }>(done => {
            resolve = done
          })
      )
    })

    $sessions.set([{ id: 'a', git_repo_root: '/repo' }] as never)
    $selectedStoredSessionId.set('a')
    vi.mocked(stampSessionPrBranch).mockClear()
    const pending = createOrOpenPr()
    $gatewayActivationGeneration.set($gatewayActivationGeneration.get() + 1)
    $sessions.set([{ id: 'b', git_repo_root: '/repo' }] as never)
    $selectedStoredSessionId.set('b')
    openReview('/repo')
    await Promise.resolve()
    review.shipInfo.mockClear()
    $reviewShipBusy.set(true)
    resolve({ url: 'https://example.com/old-pr' })
    await pending
    expect(stampSessionPrBranch).not.toHaveBeenCalled()
    expect(window.hermesDesktop?.openExternal).not.toHaveBeenCalled()
    expect(review.shipInfo).not.toHaveBeenCalled()
    expect($reviewShipBusy.get()).toBe(true)
  })
  it('commitChanges commits the trimmed message and toggles the busy flag', async () => {
    const review = stubReview()
    const seen: boolean[] = []
    const unsub = $reviewShipBusy.subscribe(v => seen.push(v))

    await commitChanges('  a message  ', { push: true })

    expect(review.commit).toHaveBeenCalledWith('/repo', 'a message', true)
    expect(seen).toContain(true)
    expect($reviewShipBusy.get()).toBe(false)
    unsub()
  })

  it('commitChanges bails on a blank message', async () => {
    const review = stubReview()
    await commitChanges('   ')
    expect(review.commit).not.toHaveBeenCalled()
  })

  it('pushChanges pushes and refreshes ship info', async () => {
    const review = stubReview()
    await pushChanges()
    expect(review.push).toHaveBeenCalledWith('/repo')
  })

  it('createOrOpenPr opens the existing PR without creating a new one', async () => {
    const review = stubReview()
    $reviewShipInfo.set({ ghReady: true, pr: { url: 'https://example.com/pr/9' } } as HermesReviewShipInfo)

    await createOrOpenPr()

    expect(review.createPr).not.toHaveBeenCalled()
    expect(
      (window.hermesDesktop as unknown as { openExternal: ReturnType<typeof vi.fn> }).openExternal
    ).toHaveBeenCalledWith('https://example.com/pr/9')
  })

  it('createOrOpenPr creates a PR when none exists, then opens it', async () => {
    const review = stubReview({ createPr: vi.fn(async () => ({ url: 'https://example.com/pr/new' })) })
    $reviewShipInfo.set({ ghReady: true, pr: null })

    await createOrOpenPr()

    expect(review.createPr).toHaveBeenCalledWith('/repo')
    expect(
      (window.hermesDesktop as unknown as { openExternal: ReturnType<typeof vi.fn> }).openExternal
    ).toHaveBeenCalledWith('https://example.com/pr/new')
  })
})

describe('refreshShipInfo', () => {
  it('populates ship info from the bridge', async () => {
    const info: HermesReviewShipInfo = {
      ghReady: true,
      pr: { url: 'https://example.com/pr/3' }
    } as HermesReviewShipInfo

    stubReview({ shipInfo: vi.fn(async () => info) })

    await refreshShipInfo()

    expect($reviewShipInfo.get()).toEqual(info)
  })

  it('resets ship info when there is no bridge', async () => {
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    $reviewShipInfo.set({ ghReady: true, pr: { url: 'x' } } as HermesReviewShipInfo)

    await refreshShipInfo()

    expect($reviewShipInfo.get()).toEqual({ ghReady: false, pr: null })
  })

  it('resets ship info when the bridge throws', async () => {
    stubReview({
      shipInfo: vi.fn(async () => {
        throw new Error('gh missing')
      })
    })
    $reviewShipInfo.set({ ghReady: true, pr: { url: 'x' } } as HermesReviewShipInfo)

    await refreshShipInfo()

    expect($reviewShipInfo.get()).toEqual({ ghReady: false, pr: null })
  })
})

describe('generateCommitMessage', () => {
  it('returns a one-shot message from the working-tree diff', async () => {
    stubReview()

    const msg = await generateCommitMessage('avoid this')

    expect(msg).toBe('generated message')
    expect(requestOneShot).toHaveBeenCalledWith(
      expect.objectContaining({
        template: 'commit_message',
        variables: expect.objectContaining({ avoid: 'avoid this', diff: 'd', recent_commits: 'r' })
      })
    )
    expect($reviewCommitMsgBusy.get()).toBe(false)
  })

  it('returns empty (no model call) when the diff is blank', async () => {
    stubReview({ commitContext: vi.fn(async () => ({ diff: '   ', recent: '' })) })

    const msg = await generateCommitMessage()

    expect(msg).toBe('')
    expect(requestOneShot).not.toHaveBeenCalled()
  })

  it('returns empty when the bridge lacks commitContext', async () => {
    const review = stubReview()
    delete review.commitContext

    const msg = await generateCommitMessage()

    expect(msg).toBe('')
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void

  const promise = new Promise<T>((done, fail) => {
    resolve = done
    reject = fail
  })

  return { promise, resolve, reject }
}

const ownerChanges = {
  config: () => {
    projectFilesystemConfigWritten()
  },
  'config roundtrip': () => {
    projectFilesystemConfigWritten()
    setProjectFilesystemScope('local')
  },
  cwd: () => {
    $currentCwd.set('/other')
    $currentCwd.set('/repo')
  },
  scope: () => {
    $reviewScopeCwd.set('/other')
    $reviewScopeCwd.set(null)
  },
  target: () => {
    $reviewScopeTarget.set('tile:other')
    $reviewScopeTarget.set('main')
  },
  session: () => {
    const id = $selectedStoredSessionId.get()
    $selectedStoredSessionId.set('other')
    $selectedStoredSessionId.set(id)
  }
}

describe.each(Object.entries(ownerChanges))('review owner invalidation: %s', (_name, changeOwner) => {
  it.each(['resolve', 'reject'])('does not revive diff %s or its finally', async outcome => {
    const result = deferred<string>()
    stubReview({ diff: vi.fn(() => result.promise) })
    const pending = selectReviewFile(file('a.ts'))
    changeOwner()
    $reviewSelectedPath.set('a.ts')
    $reviewDiff.set('current diff')
    $reviewDiffLoading.set(true)

    if (outcome === 'resolve') {
      result.resolve('stale diff')
    } else {
      result.reject(new Error('stale failure'))
    }

    await pending
    expect($reviewDiff.get()).toBe('current diff')
    expect($reviewDiffLoading.get()).toBe(true)
  })

  it('does not open or stamp a stale PR or clear current busy state', async () => {
    const result = deferred<{ url: string }>()
    const review = stubReview({ createPr: vi.fn(() => result.promise) })
    $sessions.set([{ id: 'a', git_repo_root: '/repo' }] as never)
    $selectedStoredSessionId.set('a')
    vi.mocked(stampSessionPrBranch).mockClear()
    const pending = createOrOpenPr()
    changeOwner()
    await Promise.resolve()
    review.shipInfo.mockClear()
    $reviewShipBusy.set(true)
    result.resolve({ url: 'https://example.com/stale' })
    await pending
    expect(window.hermesDesktop?.openExternal).not.toHaveBeenCalled()
    expect(stampSessionPrBranch).not.toHaveBeenCalled()
    expect(review.shipInfo).not.toHaveBeenCalled()
    expect($reviewShipBusy.get()).toBe(true)
  })

  it.each(['resolve', 'reject'])('does not publish ship info %s after owner moves while closed', async outcome => {
    const result = deferred<HermesReviewShipInfo>()
    stubReview({ shipInfo: vi.fn(() => result.promise) })
    const pending = refreshShipInfo()
    changeOwner()
    const current = { ghReady: true, pr: { url: 'https://example.com/current' } } as HermesReviewShipInfo
    $reviewShipInfo.set(current)

    if (outcome === 'resolve') {
      result.resolve({ ghReady: false, pr: null })
    } else {
      result.reject(new Error('stale ship info failure'))
    }

    await pending
    expect($reviewShipInfo.get()).toEqual(current)
  })

  it('abandons commit-message context when ownership changes', async () => {
    const result = deferred<{ diff: string; recent: string }>()
    stubReview({ commitContext: vi.fn(() => result.promise) })
    const pending = generateCommitMessage()
    changeOwner()
    $reviewCommitMsgBusy.set(true)
    result.resolve({ diff: 'stale diff', recent: 'stale history' })
    expect(await pending).toBe('')
    expect(requestOneShot).not.toHaveBeenCalled()
    expect($reviewCommitMsgBusy.get()).toBe(true)
  })

  it.each(['resolve', 'reject'])('does not publish list %s or finally after owner moves', async outcome => {
    const result = deferred<{ files: HermesReviewFile[] }>()
    stubReview({ list: vi.fn(() => result.promise) })
    $reviewOpen.set(true)
    const pending = refreshReview()
    changeOwner()
    $reviewFiles.set([file('current.ts')])
    $reviewLoading.set(true)

    if (outcome === 'resolve') {
      result.resolve({ files: [file('stale.ts')] })
    } else {
      result.reject(new Error('stale list failure'))
    }

    await pending
    expect($reviewFiles.get()).toEqual([file('current.ts')])
    expect($reviewLoading.get()).toBe(true)
  })

  it.each(['stage', 'unstage', 'revert', 'commit'] as const)(
    'does not continue %s after its list refresh loses ownership',
    async operation => {
      const result = deferred<{ files: HermesReviewFile[] }>()
      const review = stubReview({ list: vi.fn(() => result.promise) })
      $reviewOpen.set(true)

      const actions = {
        stage: () => stageReviewFile('a.ts'),
        unstage: () => unstageReviewFile('a.ts'),
        revert: () => revertReviewFile('a.ts'),
        commit: () => commitChanges('message')
      }

      const pending = actions[operation]()
      await Promise.resolve()
      expect(review.list).toHaveBeenCalled()
      changeOwner()
      await Promise.resolve()
      $reviewSelectedPath.set('a.ts')
      $reviewFiles.set([file('current.ts')])
      $reviewLoading.set(true)
      review.shipInfo.mockClear()
      vi.mocked(refreshRepoStatus).mockClear()
      result.resolve({ files: [file('a.ts')] })
      await pending
      expect($reviewFiles.get()).toEqual([file('current.ts')])
      expect($reviewLoading.get()).toBe(true)
      expect(review.diff).not.toHaveBeenCalled()
      expect(review.shipInfo).not.toHaveBeenCalled()
      expect(refreshRepoStatus).not.toHaveBeenCalled()
    }
  )

  it.each(['stage', 'unstage', 'revert', 'commit', 'push'] as const)(
    'does not run refresh continuations after pending %s',
    async operation => {
      const result = deferred<void>()
      const review = stubReview({ [operation]: vi.fn(() => result.promise) })
      $reviewOpen.set(true)

      const actions = {
        stage: () => stageReviewFile('a.ts'),
        unstage: () => unstageReviewFile('a.ts'),
        revert: () => revertReviewFile('a.ts'),
        commit: () => commitChanges('message'),
        push: pushChanges
      }

      const pending = actions[operation]()
      changeOwner()
      await Promise.resolve()
      review.list.mockClear()
      review.shipInfo.mockClear()
      vi.mocked(refreshRepoStatus).mockClear()
      $reviewShipBusy.set(true)
      result.resolve()
      await pending
      expect(review.list).not.toHaveBeenCalled()
      expect(review.shipInfo).not.toHaveBeenCalled()
      expect(refreshRepoStatus).not.toHaveBeenCalled()
      expect($reviewShipBusy.get()).toBe(true)
    }
  )
})

describe('$reviewCommitDefault', () => {
  it('remembers the split-button default action', () => {
    $reviewCommitDefault.set('commitPush')
    expect($reviewCommitDefault.get()).toBe('commitPush')
    $reviewCommitDefault.set('commit')
    expect($reviewCommitDefault.get()).toBe('commit')
  })
})
