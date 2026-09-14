import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { NO_PROJECT_ID, type SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { setProjectFilesystemScope } from '@/lib/project-filesystem-capability'
import { $sidebarAgentsGrouped, setSidebarAgentsGrouped } from '@/store/layout'
import { $activeGatewayProfile, $profileScope, ALL_PROFILES, setShowAllProfiles } from '@/store/profile'
import { $currentCwd, $selectedStoredSessionId, $sessions, applyConfiguredDefaultProjectDir } from '@/store/session'

import {
  $activeProjectId,
  $projectDialog,
  $projects,
  $projectScope,
  $projectsRpcAvailable,
  $projectTree,
  $startWorkSessionRequest,
  $worktreeDialog,
  $worktreeRefreshToken,
  addProjectFolder,
  ALL_PROJECTS,
  captureProjectPathContext,
  createProject,
  deleteProject,
  enterProject,
  exitProjectScope,
  fetchProjectSessions,
  invalidateProjectFilesystemCapabilities,
  moveSessionToProject,
  openFolderAsProject,
  openProjectCreate,
  pickProjectFolder,
  projectFilesystemScope,
  projectIdForCwd,
  projectNameForCwd,
  projectRootCwd,
  refreshProjects,
  refreshProjectTree,
  refreshWorktrees,
  renameProject,
  requestStartWorkSession,
  resolveNewSessionCwd,
  scanAndRecordRepos,
  setProjectAppearance,
  startWorkInRepo
} from './projects'
import {
  $removedSessionIds,
  $sessionMutationsInFlight,
  beginSessionMutation,
  endSessionMutation,
  tombstoneSessions
} from './session-removal'

vi.mock('@/i18n', () => ({
  translateNow: (key: string) => key
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn()
}))

vi.mock('@/lib/desktop-fs', () => ({
  cancelDesktopFsRemotePicker: vi.fn(),
  captureDesktopFsWriteRoute: vi.fn(() => ({ connectionId: 'host-a', profile: 'default', remote: true })),
  desktopDefaultCwd: vi.fn(),
  isDesktopFsRemoteMode: vi.fn(),
  projectPathEntryMode: vi.fn((scope: string | null, remote: boolean) =>
    scope === 'local' ? (remote ? 'gateway-picker' : 'native-picker') : 'text'
  ),
  selectDesktopPaths: vi.fn(),
  writeDesktopFileText: vi.fn()
}))

vi.mock('@/store/gateway', () => ({
  $gatewayActivationGeneration: atom(0),
  $gateway: atom(null),
  activeGateway: vi.fn(),
  ensureActiveGatewayOpen: vi.fn(),
  gatewayActivationEpoch: vi.fn(() => 7)
}))

vi.mock('@/lib/desktop-git', async importOriginal => ({
  ...((await importOriginal()) as Record<string, unknown>),
  desktopGit: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getHermesConfig: vi.fn(),
  getProfiles: vi.fn(),
  hermesApi: vi.fn(),
  setApiRequestProfile: vi.fn(),
  STARTUP_REQUEST_TIMEOUT_MS: 1000
}))

const fs = await import('@/lib/desktop-fs')
const captureDesktopFsWriteRoute = vi.mocked(fs.captureDesktopFsWriteRoute)
const desktopDefaultCwd = vi.mocked(fs.desktopDefaultCwd)
const isDesktopFsRemoteMode = vi.mocked(fs.isDesktopFsRemoteMode)
const selectDesktopPaths = vi.mocked(fs.selectDesktopPaths)
const writeDesktopFileText = vi.mocked(fs.writeDesktopFileText)

const gw = await import('@/store/gateway')
const activeGateway = vi.mocked(gw.activeGateway)
const gatewayAtom = gw.$gateway
const gatewayGenerationAtom = gw.$gatewayActivationGeneration

const git = await import('@/lib/desktop-git')
const desktopGit = vi.mocked(git.desktopGit)

const hermes = await import('@/hermes')
const getHermesConfig = vi.mocked(hermes.getHermesConfig)
const notifications = await import('@/store/notifications')
const notify = vi.mocked(notifications.notify)

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void

  const promise = new Promise<T>((done, fail) => {
    resolve = done
    reject = fail
  })

  return { promise, reject, resolve }
}

describe('project scope', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $projectScope.set(ALL_PROJECTS)
  })

  it('defaults to ALL_PROJECTS', () => {
    expect($projectScope.get()).toBe(ALL_PROJECTS)
  })

  it('enterProject scopes the sidebar to the project id', () => {
    // setActiveProject fires best-effort (no gateway in test → it rejects and is
    // swallowed); the synchronous scope change is what matters here.
    enterProject('p_123')
    expect($projectScope.get()).toBe('p_123')
  })

  it('exitProjectScope returns to the overview', () => {
    enterProject('p_123')
    exitProjectScope()
    expect($projectScope.get()).toBe(ALL_PROJECTS)
  })

  it('entering the synthetic Home bucket still scopes (no active pin)', () => {
    enterProject(NO_PROJECT_ID)
    expect($projectScope.get()).toBe(NO_PROJECT_ID)
  })

  it('persists the scope to localStorage', () => {
    enterProject('p_abc')
    expect(window.localStorage.getItem('hermes.desktop.projectScope')).toBe('p_abc')
  })
})

describe('projects RPC profile forwarding', () => {
  it('distinguishes a failed drill-in from an empty project', async () => {
    const failure = new Error('gateway read failed')
    const request = vi.fn().mockRejectedValueOnce(failure).mockResolvedValueOnce({ project: null })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as unknown as ReturnType<typeof activeGateway>)
    await expect(fetchProjectSessions('p_123')).rejects.toBe(failure)
    await expect(fetchProjectSessions('p_123')).resolves.toBeNull()
  })

  beforeEach(() => {
    vi.clearAllMocks()
    $activeGatewayProfile.set('default')
    $activeProjectId.set(null)
    $projectTree.set([])
    setShowAllProfiles(false)
  })

  it('forwards the normalized active profile to project read RPCs', async () => {
    const request = vi.fn(async () => ({ active_id: null, projects: [], scoped_session_ids: [] }))
    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    gatewayAtom.set(gateway as never)
    $activeGatewayProfile.set('  coder  ')

    await refreshProjects()
    await refreshProjectTree()
    await fetchProjectSessions('p_123')

    const projectReads = (request.mock.calls as unknown as Array<[string, Record<string, unknown>]>).filter(
      ([method]) => method !== 'projects.capabilities'
    )

    expect(projectReads[0]).toEqual(['projects.list', { profile: 'coder' }])
    expect(projectReads[1]).toEqual(['projects.tree', { preview_limit: 3, profile: 'coder' }])
    expect(projectReads[2]).toEqual(['projects.project_sessions', { profile: 'coder', project_id: 'p_123' }])
  })

  it('skips project reads in the all-profiles view rather than forwarding its sentinel', async () => {
    const request = vi.fn()
    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    gatewayAtom.set(gateway as never)
    setShowAllProfiles(true)
    request.mockClear()

    await refreshProjects()
    await refreshProjectTree()
    await fetchProjectSessions('p_123')

    expect(request).not.toHaveBeenCalled()
    setShowAllProfiles(false)
  })
})

describe('resolveNewSessionCwd', () => {
  beforeEach(() => {
    $projectScope.set(ALL_PROJECTS)
    applyConfiguredDefaultProjectDir('/home/user/configured')
    $currentCwd.set('')
    $selectedStoredSessionId.set(null)
    $sessions.set([])
    // Reset focused-session projections by clearing the inputs they read.
    // $focusedStoredSessionId falls back to $selectedStoredSessionId.
    // $focusedSessionState needs a runtime — leave it empty via no session states.
  })

  afterEach(() => {
    applyConfiguredDefaultProjectDir(null)
    $projectScope.set(ALL_PROJECTS)
    $currentCwd.set('')
    $selectedStoredSessionId.set(null)
    $sessions.set([])
  })

  it('starts a chat detached inside Home, ignoring the configured default dir', () => {
    // Attaching the default dir here would move the new chat out of Home the
    // moment it was created — "no folder" is what the bucket means.
    enterProject(NO_PROJECT_ID)

    expect(resolveNewSessionCwd()).toBe('')
  })

  it('still falls back to the configured default outside Home', () => {
    expect(resolveNewSessionCwd()).toBe('/home/user/configured')
  })

  it('does not inherit the focused session workspace — new chat uses the configured default', () => {
    // Regression for #71873 / #80213: after a restart the focused session is
    // usually the just-resumed one, whose stored cwd can be a stale fallback
    // (e.g. the user's home dir on Windows). A new chat must NOT land there —
    // it falls through to the configured default project dir.
    $selectedStoredSessionId.set('sess-a')
    $sessions.set([
      {
        archived: false,
        cwd: 'C:\\Users\\sonny',
        ended_at: null,
        id: 'sess-a',
        input_tokens: 0,
        is_active: true,
        last_active: 0,
        message_count: 1,
        model: null,
        output_tokens: 0,
        started_at: 0,
        title: 'work'
      } as never
    ])

    expect(resolveNewSessionCwd()).toBe('/home/user/configured')
  })

  it('does not re-attach a remembered cwd when the focused session is detached', () => {
    $currentCwd.set('/Users/me/stale-remembered')
    $selectedStoredSessionId.set('sess-detached')
    $sessions.set([
      {
        archived: false,
        cwd: null,
        ended_at: null,
        id: 'sess-detached',
        input_tokens: 0,
        is_active: true,
        last_active: 0,
        message_count: 1,
        model: null,
        output_tokens: 0,
        started_at: 0,
        title: 'loose'
      } as never
    ])

    // Focused session has no workspace → fall through to configured default,
    // not the stale $currentCwd from an earlier chat.
    expect(resolveNewSessionCwd()).toBe('/home/user/configured')
  })
})

describe('projectNameForCwd', () => {
  const treeNode = (
    over: Partial<SidebarProjectTree> & Pick<SidebarProjectTree, 'id' | 'label'>
  ): SidebarProjectTree => ({
    path: null,
    repos: [],
    sessionCount: 0,
    ...over
  })

  beforeEach(() => {
    $projectTree.set([])
  })

  it('names the explicit project owning the cwd (longest path match)', () => {
    $projectTree.set([
      treeNode({ id: 'p_web', label: 'Website', path: '/repos/website' }),
      treeNode({ id: 'p_api', label: 'API', path: '/repos/api' })
    ])

    expect(projectNameForCwd('/repos/website/src/app')).toBe('Website')
  })

  it('matches nested repo and worktree paths, not just the project root', () => {
    $projectTree.set([
      treeNode({
        id: 'p_mono',
        label: 'Monorepo',
        path: '/repos/mono',
        repos: [
          {
            id: 'r1',
            label: 'mono',
            path: '/repos/mono',
            sessionCount: 0,
            groups: [{ id: 'g1', label: 'feature', path: '/elsewhere/mono-feature', sessions: [] }]
          }
        ]
      })
    ])

    // A linked worktree lives OUTSIDE the project root but still belongs to it.
    expect(projectNameForCwd('/elsewhere/mono-feature/src')).toBe('Monorepo')
  })

  it('matches nested Windows paths across separator and case differences', () => {
    $projectTree.set([treeNode({ id: 'p_win', label: 'Windows app', path: 'C:\\Repos\\App' })])

    expect(projectIdForCwd('c:/repos/app/src')).toBe('p_win')
    expect(projectNameForCwd('c:/repos/app/src')).toBe('Windows app')
  })

  it('ignores auto-projects and the No-project bucket (no named identity)', () => {
    $projectTree.set([
      treeNode({ id: '/repos/loose', label: 'loose', path: '/repos/loose', isAuto: true }),
      treeNode({ id: '__no_project__', label: 'No project', path: null, isNoProject: true })
    ])

    expect(projectNameForCwd('/repos/loose/src')).toBeNull()
  })

  it('returns null for a cwd in no project and for a blank cwd', () => {
    $projectTree.set([treeNode({ id: 'p_web', label: 'Website', path: '/repos/website' })])

    expect(projectNameForCwd('/somewhere/else')).toBeNull()
    expect(projectNameForCwd('')).toBeNull()
  })
})

describe('worktree refresh', () => {
  it('refreshWorktrees bumps the probe token so useRepoWorktreeMap refetches', () => {
    const before = $worktreeRefreshToken.get()
    refreshWorktrees()
    expect($worktreeRefreshToken.get()).toBe(before + 1)
  })
})

describe('startWorkInRepo remote capability gate (#81724)', () => {
  it('names the stale-backend remedy when a remote gateway lacks the worktree route', async () => {
    isDesktopFsRemoteMode.mockReturnValue(true)
    desktopGit.mockReturnValue({
      worktreeAdd: vi.fn(async () => {
        throw new Error(
          'Expected JSON from https://vps/api/git/worktree/add but got HTML (status 404). The endpoint is likely missing on the Hermes backend.'
        )
      })
    } as never)

    // The i18n mock echoes keys, so the surfaced error is the catalog key.
    await expect(startWorkInRepo('/srv/repo', { branch: 'x' })).rejects.toThrow('sidebar.projects.worktreeStaleBackend')
  })

  it('re-throws real git failures untouched (a remote 400 is not a capability verdict)', async () => {
    isDesktopFsRemoteMode.mockReturnValue(true)
    desktopGit.mockReturnValue({
      worktreeAdd: vi.fn(async () => {
        throw new Error("400: fatal: 'stale' is not a commit")
      })
    } as never)

    await expect(startWorkInRepo('/srv/repo', { branch: 'x' })).rejects.toThrow('not a commit')
  })
})

describe('pickProjectFolder', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $activeGatewayProfile.set('default')
  })

  it('uses the native directory picker for a local filesystem on a local connection', async () => {
    const request = vi.fn().mockResolvedValue({ filesystem_scope: 'local' })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    isDesktopFsRemoteMode.mockReturnValue(false)
    captureDesktopFsWriteRoute.mockReturnValueOnce({ profile: 'default', remote: false })
    selectDesktopPaths.mockResolvedValue(['/local/repo'])

    await expect(pickProjectFolder()).resolves.toBe('/local/repo')
    expect(request).toHaveBeenCalledWith('projects.capabilities', { profile: 'default' })
    expect(selectDesktopPaths).toHaveBeenCalledWith(
      { defaultPath: undefined, directories: true, multiple: false },
      { profile: 'default', remote: false }
    )
  })

  it('uses the gateway picker for a local filesystem on a remote connection', async () => {
    const request = vi.fn().mockResolvedValue({ filesystem_scope: 'local' })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    isDesktopFsRemoteMode.mockReturnValue(true)
    desktopDefaultCwd.mockResolvedValue({ branch: 'main', cwd: '/backend/work' })
    selectDesktopPaths.mockResolvedValue(['/backend/work/repo'])

    await expect(pickProjectFolder()).resolves.toBe('/backend/work/repo')
    expect(desktopDefaultCwd).toHaveBeenCalledWith({ connectionId: 'host-a', profile: 'default', remote: true })
    expect(selectDesktopPaths).toHaveBeenCalledWith(
      {
        defaultPath: '/backend/work',
        directories: true,
        multiple: false
      },
      { connectionId: 'host-a', profile: 'default', remote: true }
    )
  })

  it.each(['non_local', 'unknown'] as const)('never opens a picker for a %s filesystem', async filesystemScope => {
    const request = vi.fn().mockResolvedValue({ filesystem_scope: filesystemScope })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await expect(pickProjectFolder()).resolves.toBeNull()
    expect(selectDesktopPaths).not.toHaveBeenCalled()
    expect(desktopDefaultCwd).not.toHaveBeenCalled()
  })

  it('treats missing capability RPC as unknown and never opens a picker', async () => {
    const request = vi.fn().mockRejectedValue(new Error('unknown method: projects.capabilities'))
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    const context = captureProjectPathContext()

    await expect(projectFilesystemScope(context!)).resolves.toBe('unknown')
    await expect(pickProjectFolder(context!)).resolves.toBeNull()
    expect(selectDesktopPaths).not.toHaveBeenCalled()
  })

  it('pins and caches capability lookup to the captured gateway, profile, and generation', async () => {
    const requestA = vi.fn().mockResolvedValue({ filesystem_scope: 'local' })
    const gatewayA = { connectionState: 'open', request: requestA }
    activeGateway.mockReturnValue(gatewayA as never)
    $activeGatewayProfile.set('coder')
    const context = captureProjectPathContext()

    expect(context?.fsWriteRoute?.profile).toBe('coder')
    await expect(projectFilesystemScope(context!)).resolves.toBe('local')
    await expect(projectFilesystemScope(context!)).resolves.toBe('local')

    expect(requestA).toHaveBeenCalledOnce()
    expect(requestA).toHaveBeenCalledWith('projects.capabilities', { profile: 'coder' })
    expect(context).toMatchObject({ gateway: gatewayA, generation: 7, profile: 'coder' })
  })

  it('rejects delete confirmation captured before a config write', async () => {
    const request = vi.fn().mockResolvedValue({})
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    const context = captureProjectPathContext()!
    invalidateProjectFilesystemCapabilities()
    await expect(deleteProject('p1', context)).rejects.toThrow()
    expect(request).not.toHaveBeenCalled()
  })

  it('refetches filesystem locality after terminal configuration changes in the same profile', async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce({ filesystem_scope: 'local' })
      .mockResolvedValueOnce({ filesystem_scope: 'non_local' })

    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    const context = captureProjectPathContext()!

    await expect(projectFilesystemScope(context)).resolves.toBe('local')
    invalidateProjectFilesystemCapabilities()
    await expect(projectFilesystemScope(context)).resolves.toBe('non_local')

    expect(request).toHaveBeenCalledTimes(2)
  })

  it('does not publish an in-flight local capability after terminal configuration changes', async () => {
    const staleLocal = deferred<{ filesystem_scope: 'local' }>()

    const request = vi
      .fn()
      .mockReturnValueOnce(staleLocal.promise)
      .mockResolvedValueOnce({ filesystem_scope: 'non_local' })

    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    const context = captureProjectPathContext()!

    const pending = projectFilesystemScope(context)
    invalidateProjectFilesystemCapabilities()
    staleLocal.resolve({ filesystem_scope: 'local' })

    await expect(pending).resolves.toBe('non_local')
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('retries unknown and failed filesystem locality lookups', async () => {
    const request = vi
      .fn()
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce({ filesystem_scope: 'unknown' })
      .mockResolvedValueOnce({ filesystem_scope: 'local' })

    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    const context = captureProjectPathContext()!

    await expect(projectFilesystemScope(context)).resolves.toBe('unknown')
    await expect(projectFilesystemScope(context)).resolves.toBe('unknown')
    await expect(projectFilesystemScope(context)).resolves.toBe('local')

    expect(request).toHaveBeenCalledTimes(3)
  })
})

describe('createProject', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setSidebarAgentsGrouped(false)
    $activeProjectId.set(null)
    $projectsRpcAvailable.set(null)
    $projects.set([])
    $projectTree.set([])
    $activeGatewayProfile.set('default')
    setShowAllProfiles(false)
  })

  afterEach(() => {
    setShowAllProfiles(false)
    $activeGatewayProfile.set('default')
  })

  it.each(['default', 'coder'])('creates in the active %s profile without leaving All profiles', async profile => {
    const created = { folders: [], id: 'p_new', name: 'Hermes Agent', primary_path: '/srv/hermes' }
    const tree = { id: created.id, label: created.name, path: created.primary_path, repos: [], sessionCount: 0 }
    const request = vi.fn().mockResolvedValue({ project: created })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    vi.mocked(hermes.hermesApi).mockResolvedValue({ projects: [tree], active_id: created.id })
    $activeGatewayProfile.set(profile)
    setShowAllProfiles(true)

    await expect(createProject({ folders: ['/srv/hermes'], name: created.name, use: true })).resolves.toEqual(created)

    expect(request).toHaveBeenCalledWith('projects.create', expect.objectContaining({ profile, name: created.name }))
    expect($profileScope.get()).toBe(ALL_PROFILES)
    expect($projects.get()).toContainEqual(created)
    expect($projectTree.get()).toEqual(expect.arrayContaining([expect.objectContaining({ id: created.id })]))
    expect($activeProjectId.get()).toBe(created.id)
    expect(hermes.hermesApi).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/api/profiles/projects/tree?preview_limit=3' })
    )
  })

  it('does not retarget a project create when the profile changes during reconnect', async () => {
    const reconnect = deferred<never>()
    const request = vi.fn()
    activeGateway.mockReturnValue({ connectionState: 'closed', request } as never)
    vi.mocked(gw.ensureActiveGatewayOpen).mockReturnValue(reconnect.promise)
    $activeGatewayProfile.set('coder')
    setShowAllProfiles(true)

    const pending = createProject({ folders: ['/srv/hermes'], name: 'Hermes Agent' })
    const rejection = expect(pending).rejects.toThrow('Active Hermes profile changed while connecting')
    const otherGateway = { connectionState: 'open', request }
    $activeGatewayProfile.set('other')
    activeGateway.mockReturnValue(otherGateway as never)
    reconnect.resolve(otherGateway as never)

    await rejection
    expect(request).not.toHaveBeenCalled()
  })

  it('creates the project and flips into the grouped view so a blank slate shows it', async () => {
    const created = { folders: [], id: 'p_new', name: 'Demo', primary_path: '/srv/demo' }

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.create') {
        return { project: created }
      }

      // Reconcile (fire-and-forget) re-reads list + tree; echo the project back
      // so the optimistic state survives instead of being wiped to empty.
      return { active_id: 'p_new', projects: [created], scoped_session_ids: [] }
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    const result = await createProject({ folders: ['/srv/demo'], name: 'Demo', use: true })

    expect(result).toEqual(created)
    expect(request).toHaveBeenCalledWith('projects.create', expect.objectContaining({ name: 'Demo' }))
    expect($sidebarAgentsGrouped.get()).toBe(true)
    expect($activeProjectId.get()).toBe('p_new')
  })

  it('marks the backend stale and surfaces a friendly error when projects.create is missing', async () => {
    activeGateway.mockReturnValue({
      connectionState: 'open',
      request: vi.fn().mockRejectedValue(new Error('unknown method: projects.create'))
    } as never)

    await expect(createProject({ folders: ['/srv/demo'], name: 'Demo' })).rejects.toThrow(
      'sidebar.projects.staleBackend'
    )
    expect($projectsRpcAvailable.get()).toBe(false)
  })
})

describe('projects RPC capability', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $projectsRpcAvailable.set(null)
  })

  it('marks the backend stale when projects.list is missing', async () => {
    activeGateway.mockReturnValue({
      connectionState: 'open',
      request: vi.fn().mockRejectedValue(new Error('unknown method: projects.list'))
    } as never)

    await refreshProjects()

    expect($projectsRpcAvailable.get()).toBe(false)
  })

  it('does not publish a late project list from the previous source', async () => {
    let resolveA: ((value: unknown) => void) | undefined

    const responseA = new Promise(resolve => {
      resolveA = resolve
    })

    const gatewayA = { connectionState: 'open', request: vi.fn(() => responseA) }

    const gatewayB = {
      connectionState: 'open',
      request: vi.fn().mockResolvedValue({ active_id: null, projects: [{ id: 'source-b', name: 'Source B' }] })
    }

    let current = gatewayA

    activeGateway.mockImplementation(() => current as never)
    const pendingA = refreshProjects()

    current = gatewayB
    await refreshProjects()

    resolveA?.({ active_id: null, projects: [{ id: 'source-a', name: 'Source A' }] })
    await pendingA

    expect($projects.get().map(project => project.id)).toEqual(['source-b'])
  })

  it('blocks opening the create dialog once the backend is known stale', () => {
    $projectsRpcAvailable.set(false)

    openProjectCreate()

    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'warning', message: 'sidebar.projects.staleBackend' })
    )
  })
})

describe('repository discovery policy', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $activeGatewayProfile.set('default')
    isDesktopFsRemoteMode.mockReturnValue(false)
  })

  function gatewayWith(request: ReturnType<typeof vi.fn>) {
    const routedRequest = vi.fn((method: string, params?: Record<string, unknown>) =>
      method === 'projects.capabilities'
        ? Promise.resolve({ filesystem_scope: 'local' })
        : (request as (method: string, params?: Record<string, unknown>) => Promise<unknown>)(method, params)
    )

    const gateway = { connectionState: 'open', request: routedRequest }
    activeGateway.mockReturnValue(gateway as never)
    gatewayAtom.set(gateway as never)
    setProjectFilesystemScope('local')

    return gateway
  }

  it('records disabled policy without invoking the filesystem scanner', async () => {
    const request = vi.fn(async (method: string) =>
      method === 'projects.tree'
        ? { active_id: null, projects: [], scoped_session_ids: [] }
        : { accepted: false, repos: [] }
    )

    gatewayWith(request)
    const scanRepos = vi.fn()
    desktopGit.mockReturnValue({ scanRepos } as never)
    getHermesConfig.mockResolvedValue({
      desktop: {
        repo_scan_enabled: false,
        repo_scan_exclude_paths: [],
        repo_scan_roots: []
      }
    })

    await scanAndRecordRepos()

    expect(scanRepos).not.toHaveBeenCalled()
    expect(request).toHaveBeenCalledWith('projects.record_repos', {
      discovery_policy: { enabled: false, exclude_paths: [], roots: [] },
      profile: 'default',
      repos: []
    })
  })

  it('passes custom roots and exclusions to Electron and records on the origin gateway', async () => {
    const request = vi.fn(async (method: string) =>
      method === 'projects.tree'
        ? { active_id: null, projects: [], scoped_session_ids: [] }
        : { accepted: true, repos: [] }
    )

    gatewayWith(request)
    const scanRepos = vi.fn().mockResolvedValue([{ label: 'repo', root: '/work/repo' }])
    desktopGit.mockReturnValue({ scanRepos } as never)
    getHermesConfig.mockResolvedValue({
      desktop: {
        repo_scan_enabled: true,
        repo_scan_exclude_paths: ['/work/vendor'],
        repo_scan_roots: ['/work']
      }
    })

    await scanAndRecordRepos()

    expect(getHermesConfig).toHaveBeenCalledWith('default')
    expect(scanRepos).toHaveBeenCalledWith(['/work'], {
      enabled: true,
      excludePaths: ['/work/vendor']
    })
    expect(request).toHaveBeenCalledWith('projects.record_repos', {
      discovery_policy: {
        enabled: true,
        exclude_paths: ['/work/vendor'],
        roots: ['/work']
      },
      profile: 'default',
      repos: [{ label: 'repo', root: '/work/repo' }]
    })
  })

  it('does not scan the local filesystem for remote connections but still refreshes the project tree', async () => {
    isDesktopFsRemoteMode.mockReturnValue(true)
    const scanRepos = vi.fn()
    desktopGit.mockReturnValue({ scanRepos } as never)

    const request = vi.fn(async (method: string) =>
      method === 'projects.tree'
        ? { active_id: null, projects: [], scoped_session_ids: [] }
        : { accepted: false, repos: [] }
    )

    gatewayWith(request)
    $projectTree.set([
      { id: 'seed', label: 'seed', path: null, repos: [], sessionCount: 0 } satisfies SidebarProjectTree
    ])

    await scanAndRecordRepos(true)

    expect(scanRepos).not.toHaveBeenCalled()
    expect(getHermesConfig).not.toHaveBeenCalled()
    // The desktop can't crawl the remote host's filesystem, so it asks the
    // host to scan its own discovery roots (`projects.discover_repos` with
    // `scan: true`) — repos with zero Hermes sessions must still surface —
    // then refreshes the tree to pick up the merged list. Regression for
    // #81723: the sidebar used to go silent in remote mode and never
    // refresh again.
    expect(request).toHaveBeenCalledWith('projects.discover_repos', { profile: 'default', scan: true })
    expect(request).toHaveBeenCalledWith(
      'projects.tree',
      expect.objectContaining({ preview_limit: expect.any(Number), profile: 'default' })
    )
    // A successful scan refreshes the tree (here to the empty list the mock
    // tree returns), so a later discover-repos call replaces it instead of
    // keeping the stale seed.
    expect($projectTree.get()).toEqual([])
  })

  it('surfaces a reject from remote discover_repos without clearing the sidebar', async () => {
    // Backend error (RPC `error` frame) rejects the request — the sidebar must
    // keep its last known list and flag the failure, not go silently blank.
    isDesktopFsRemoteMode.mockReturnValue(true)
    desktopGit.mockReturnValue({ scanRepos: vi.fn() } as never)

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.discover_repos') {
        throw new Error('discover_repos failed')
      }

      if (method === 'projects.tree') {
        return { active_id: null, projects: [], scoped_session_ids: [] }
      }

      return { accepted: false, repos: [] }
    })

    gatewayWith(request)
    $projectTree.set([
      { id: 'seed', label: 'seed', path: null, repos: [], sessionCount: 0 } satisfies SidebarProjectTree
    ])

    await scanAndRecordRepos(true)

    // The tree refresh must NOT run against a failed remote scan ...
    expect(request).not.toHaveBeenCalledWith(
      'projects.tree',
      expect.objectContaining({ preview_limit: expect.any(Number) })
    )
    // ... the cached tree is preserved ...
    expect($projectTree.get()).toEqual([{ id: 'seed', label: 'seed', path: null, repos: [], sessionCount: 0 }])
  })

  it('does not treat an error-shaped discover_repos response as a successful refresh', async () => {
    // A resolved-but-error-shaped body (`{accepted:false}` / no `repos`) must
    // be treated as a failure: keep the old list rather than refreshing into
    // the silent, empty sidebar of #81723.
    isDesktopFsRemoteMode.mockReturnValue(true)
    desktopGit.mockReturnValue({ scanRepos: vi.fn() } as never)

    const request = vi.fn(async (method: string) =>
      method === 'projects.tree' ? { active_id: null, projects: [], scoped_session_ids: [] } : { accepted: false }
    )

    gatewayWith(request)
    $projectTree.set([
      { id: 'seed', label: 'seed', path: null, repos: [], sessionCount: 0 } satisfies SidebarProjectTree
    ])

    await scanAndRecordRepos(true)

    expect(request).not.toHaveBeenCalledWith(
      'projects.tree',
      expect.objectContaining({ preview_limit: expect.any(Number) })
    )
    expect($projectTree.get()).toEqual([{ id: 'seed', label: 'seed', path: null, repos: [], sessionCount: 0 }])
  })

  it('records repos under the profile the scan started with, not one focused mid-scan', async () => {
    const { promise: scanResult, resolve: resolveScan } = deferred<Array<{ label: string; root: string }>>()
    const { promise: scanStarted, resolve: markScanStarted } = deferred<void>()

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'local' }
      }

      return method === 'projects.tree'
        ? {
            active_id: null,
            projects: [{ id: 'p_lured', label: 'Lured', path: null, repos: [], sessionCount: 0 }],
            scoped_session_ids: []
          }
        : { accepted: true, repos: [] }
    })

    gatewayWith(request)

    const scanRepos = vi.fn(() => {
      markScanStarted()

      return scanResult
    })

    desktopGit.mockReturnValue({ scanRepos } as never)
    getHermesConfig.mockResolvedValue({
      desktop: {
        repo_scan_enabled: true,
        repo_scan_exclude_paths: [],
        repo_scan_roots: ['/work']
      }
    })
    $activeGatewayProfile.set('launch')
    $projectTree.set([])

    const pending = scanAndRecordRepos()
    await scanStarted
    $activeGatewayProfile.set('coder')
    resolveScan([{ label: 'repo', root: '/work/repo' }])
    await pending

    expect(request).toHaveBeenCalledWith('projects.record_repos', {
      discovery_policy: { enabled: true, exclude_paths: [], roots: ['/work'] },
      profile: 'launch',
      repos: [{ label: 'repo', root: '/work/repo' }]
    })
    expect(request).not.toHaveBeenCalledWith('projects.record_repos', expect.objectContaining({ profile: 'coder' }))
    expect($projectTree.get()).toEqual([])
  })
})

describe('project tree profile isolation', () => {
  beforeEach(() => {
    setShowAllProfiles(false)
    $activeGatewayProfile.set('default')
    $projects.set([])
    $projectTree.set([])
  })

  it('retries a dropped projects.tree request once on the active gateway', async () => {
    const request = vi
      .fn()
      .mockRejectedValueOnce(new Error('request timed out after 30s: projects.tree'))
      .mockResolvedValueOnce({
        active_id: null,
        projects: [{ id: 'remote-tree', label: 'Remote tree', path: null, repos: [], sessionCount: 0 }],
        scoped_session_ids: []
      })

    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    gatewayAtom.set(gateway as never)

    await refreshProjectTree()

    expect(request).toHaveBeenCalledTimes(2)
    expect($projectTree.get().map(project => project.id)).toEqual(['remote-tree'])
  })

  it('does not publish a late response from the previous gateway', async () => {
    let resolveA: ((value: unknown) => void) | undefined

    const responseA = new Promise(resolve => {
      resolveA = resolve
    })

    const gatewayA = { connectionState: 'open', request: vi.fn(() => responseA) }

    const gatewayB = {
      connectionState: 'open',
      request: vi.fn().mockResolvedValue({
        active_id: null,
        projects: [{ id: 'profile-b', label: 'Profile B', path: null, repos: [], sessionCount: 0 }],
        scoped_session_ids: []
      })
    }

    let current = gatewayA
    activeGateway.mockImplementation(() => current as never)
    gatewayAtom.set(gatewayA as never)

    const pendingA = refreshProjectTree()
    current = gatewayB
    $activeGatewayProfile.set('profile-b')
    gatewayAtom.set(gatewayB as never)
    await refreshProjectTree()
    resolveA?.({
      active_id: null,
      projects: [{ id: 'profile-a', label: 'Profile A', path: null, repos: [], sessionCount: 0 }],
      scoped_session_ids: []
    })
    await pendingA

    expect($projectTree.get().map(project => project.id)).toEqual(['profile-b'])
  })

  it('does not publish a late projects.list response from the previous profile', async () => {
    const { promise: defaultResponse, resolve: resolveDefault } = deferred<unknown>()

    const request = vi.fn((_method: string, params: Record<string, unknown>) =>
      params.profile === 'default'
        ? defaultResponse
        : Promise.resolve({
            active_id: null,
            projects: [{ id: 'profile-b', label: 'Profile B' }]
          })
    )

    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    gatewayAtom.set(gateway as never)

    const pendingDefault = refreshProjects()
    $activeGatewayProfile.set('profile-b')
    await refreshProjects()
    resolveDefault({
      active_id: null,
      projects: [{ id: 'profile-a', label: 'Profile A' }]
    })
    await pendingDefault

    expect($projects.get().map(project => project.id)).toEqual(['profile-b'])
  })

  it('does not publish a late projects.tree response from the previous profile', async () => {
    const { promise: defaultResponse, resolve: resolveDefault } = deferred<unknown>()

    const request = vi.fn((_method: string, params: Record<string, unknown>) =>
      params.profile === 'default'
        ? defaultResponse
        : Promise.resolve({
            active_id: null,
            projects: [{ id: 'profile-b', label: 'Profile B', path: null, repos: [], sessionCount: 0 }],
            scoped_session_ids: []
          })
    )

    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    gatewayAtom.set(gateway as never)

    const pendingDefault = refreshProjectTree()
    $activeGatewayProfile.set('profile-b')
    await refreshProjectTree()
    resolveDefault({
      active_id: null,
      projects: [{ id: 'profile-a', label: 'Profile A', path: null, repos: [], sessionCount: 0 }],
      scoped_session_ids: []
    })
    await pendingDefault

    expect($projectTree.get().map(project => project.id)).toEqual(['profile-b'])
  })

  it('drops a late hydrated-project response from the previous profile', async () => {
    const { promise: defaultResponse, resolve: resolveDefault } = deferred<unknown>()

    const request = vi.fn((_method: string, params: Record<string, unknown>) =>
      params.profile === 'default'
        ? defaultResponse
        : Promise.resolve({
            project: { id: 'profile-b', label: 'Profile B', path: null, repos: [], sessionCount: 0 }
          })
    )

    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    gatewayAtom.set(gateway as never)

    const pendingDefault = fetchProjectSessions('p_123')
    $activeGatewayProfile.set('profile-b')
    const profileB = await fetchProjectSessions('p_123')
    resolveDefault({
      project: { id: 'profile-a', label: 'Profile A', path: null, repos: [], sessionCount: 0 }
    })

    expect(profileB?.id).toBe('profile-b')
    await expect(pendingDefault).resolves.toBeNull()
  })
})

describe('tombstone pruning', () => {
  const openGatewayReturning = (scopedIds: string[]) => {
    const gateway = {
      connectionState: 'open',
      request: vi.fn().mockResolvedValue({ active_id: null, projects: [], scoped_session_ids: scopedIds })
    }

    activeGateway.mockImplementation(() => gateway as never)
    gatewayAtom.set(gateway as never)

    return gateway
  }

  beforeEach(() => {
    $removedSessionIds.set(new Set())
    $sessionMutationsInFlight.set(new Set())
  })

  it('keeps an in-flight delete tombstone even when the backend snapshot omits it', async () => {
    // Optimistic delete: hide the row, mark the RPC as in flight.
    tombstoneSessions(['sess-1'])
    beginSessionMutation(['sess-1'])

    // A projects.tree refresh races the pending delete: the id is already gone
    // from scope, but the RPC hasn't landed — the tombstone must survive so the
    // row doesn't flash back.
    openGatewayReturning([])
    await refreshProjectTree()

    expect($removedSessionIds.get().has('sess-1')).toBe(true)
  })

  it('prunes the tombstone once the mutation settles and scope no longer lists it', async () => {
    tombstoneSessions(['sess-1'])
    beginSessionMutation(['sess-1'])
    openGatewayReturning([])
    await refreshProjectTree()

    // Delete RPC settled; the next refresh with the id absent from scope drops it.
    endSessionMutation(['sess-1'])
    await refreshProjectTree()

    expect($removedSessionIds.get().has('sess-1')).toBe(false)
  })
})

describe('session workspace move response', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $activeGatewayProfile.set('default')
    $projectTree.set([{ id: 'p_remote', label: 'Remote', path: '/requested/root', repos: [], sessionCount: 1 }])
    $sessions.set([{ id: 'session-a', cwd: '/old/root' }] as never)
  })

  it('uses the canonical workspace returned by session.workspace.move', async () => {
    const request = vi
      .fn()
      .mockResolvedValue({ cwd: '/canonical/root', branch: 'main', git_repo_root: '/canonical/root' })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await moveSessionToProject('session-a', 'p_remote')

    expect($sessions.get()[0]?.cwd).toBe('/canonical/root')
  })

  it('preserves whitespace in a non-empty canonical workspace response', async () => {
    const request = vi.fn().mockResolvedValue({ cwd: '/canonical/root ', branch: '', git_repo_root: '' })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await moveSessionToProject('session-a', 'p_remote')

    expect($sessions.get()[0]?.cwd).toBe('/canonical/root ')
  })

  it('fails closed when session.workspace.move omits its canonical cwd', async () => {
    const request = vi.fn().mockResolvedValue({})
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await expect(moveSessionToProject('session-a', 'p_remote')).rejects.toThrow('canonical workspace')
    expect($sessions.get()[0]?.cwd).toBe('/old/root')
  })
})

describe('project canonical root', () => {
  it('preserves whitespace in a non-empty backend path', () => {
    const node = {
      id: 'p-space',
      label: 'Spaced',
      path: '/remote/root ',
      repos: [],
      sessionCount: 0
    } as never

    $projectTree.set([node])

    expect(projectRootCwd(node)).toBe('/remote/root ')
    expect(projectIdForCwd('/remote/root /src')).toBe('p-space')
    expect(projectNameForCwd('/remote/root /src')).toBe('Spaced')

    requestStartWorkSession('/remote/root ')
    expect($startWorkSessionRequest.get()?.path).toBe('/remote/root ')
  })
})

describe('authoritative project path mutations', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $activeGatewayProfile.set('default')
    $projectsRpcAvailable.set(null)
    $activeProjectId.set(null)
    $projectDialog.set(null)
    $projectScope.set(ALL_PROJECTS)
    $startWorkSessionRequest.set(null)
    $projects.set([])
    $projectTree.set([])
    setShowAllProfiles(false)
    isDesktopFsRemoteMode.mockReturnValue(false)
  })

  it('replaces an optimistic add-folder spelling with the authoritative project response', async () => {
    const original = {
      archived: false,
      board_slug: null,
      color: null,
      created_at: 1,
      description: null,
      folders: [],
      icon: null,
      id: 'p_remote',
      name: 'Remote',
      primary_path: null,
      slug: 'remote'
    }

    const canonical = {
      ...original,
      folders: [{ added_at: 2, is_primary: true, label: null, path: '/workspace/repo' }],
      primary_path: '/workspace/repo'
    }

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.add_folder') {
        return { project: canonical }
      }

      if (method === 'projects.list') {
        return { active_id: null, projects: [canonical] }
      }

      return { active_id: null, projects: [], scoped_session_ids: [] }
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    $projects.set([original])
    $projectTree.set([{ id: original.id, label: original.name, path: null, repos: [], sessionCount: 0 }])

    await addProjectFolder(original.id, '../repo', { isPrimary: true })

    expect(request).toHaveBeenCalledWith(
      'projects.add_folder',
      expect.objectContaining({ path: '../repo', profile: 'default' })
    )
    expect($projects.get()[0]?.primary_path).toBe('/workspace/repo')
    expect($projectTree.get()[0]?.path).toBe('/workspace/repo')
  })

  it('does not roll a stale add-folder failure back over the newly active profile', async () => {
    const mutation = deferred<{ project: null }>()

    const gatewayA = {
      connectionState: 'open',
      request: vi.fn((method: string) =>
        method === 'projects.add_folder' ? mutation.promise : Promise.resolve({ active_id: null, projects: [] })
      )
    }

    const gatewayB = { connectionState: 'open', request: vi.fn() }
    let current = gatewayA
    activeGateway.mockImplementation(() => current as never)
    const projectA = { folders: [], id: 'p_a', name: 'A', primary_path: null }
    const projectB = { folders: [], id: 'p_b', name: 'B', primary_path: null }
    const treeA = { id: 'p_a', label: 'A', path: null, repos: [], sessionCount: 0 }
    const treeB = { id: 'p_b', label: 'B', path: null, repos: [], sessionCount: 0 }
    $projects.set([projectA] as never)
    $projectTree.set([treeA])

    const pending = addProjectFolder('p_a', '/remote/repo ', { isPrimary: true })
    await vi.waitFor(() => expect(gatewayA.request).toHaveBeenCalledWith('projects.add_folder', expect.anything()))
    expect($projectTree.get()[0]?.path).toBe('/remote/repo ')
    current = gatewayB
    $activeGatewayProfile.set('profile-b')
    $projects.set([projectB] as never)
    $projectTree.set([treeB])
    mutation.resolve({ project: null })

    await expect(pending).rejects.toThrow('Active Hermes profile changed')
    expect($projects.get()).toEqual([projectB])
    expect($projectTree.get()).toEqual([treeB])
  })

  it('opens a text prompt without browsing when no path is supplied for a non-local profile', async () => {
    const request = vi.fn().mockResolvedValue({ filesystem_scope: 'non_local' })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await openFolderAsProject()

    expect($projectDialog.get()).toMatchObject({ mode: 'open-folder', context: { profile: 'default' } })
    expect(selectDesktopPaths).not.toHaveBeenCalled()
    expect(desktopDefaultCwd).not.toHaveBeenCalled()
  })

  it('submits a direct raw path and starts the session at the canonical cwd', async () => {
    const created = {
      archived: false,
      board_slug: null,
      color: null,
      created_at: 1,
      description: null,
      folders: [{ added_at: 1, is_primary: true, label: null, path: '/workspace/repo' }],
      icon: null,
      id: 'p_remote',
      name: '../repo',
      primary_path: '/workspace/repo',
      slug: 'repo'
    }

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'non_local' }
      }

      if (method === 'projects.for_cwd') {
        return { branch: '', cwd: '/workspace/repo', project: null }
      }

      if (method === 'projects.create') {
        return { project: created }
      }

      if (method === 'projects.list') {
        return { active_id: created.id, projects: [created] }
      }

      return { active_id: created.id, projects: [], scoped_session_ids: [] }
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await openFolderAsProject('../repo', undefined, 'Remote workspace')

    expect(request).toHaveBeenCalledWith('projects.for_cwd', { cwd: '../repo', profile: 'default' })
    expect(request).toHaveBeenCalledWith(
      'projects.create',
      expect.objectContaining({
        folders: ['../repo'],
        name: 'Remote workspace',
        primary_path: '../repo',
        profile: 'default'
      })
    )
    expect($projectTree.get()).toContainEqual(expect.objectContaining({ id: created.id, path: '/workspace/repo' }))
    expect($startWorkSessionRequest.get()).toMatchObject({ path: '/workspace/repo' })
    expect(selectDesktopPaths).not.toHaveBeenCalled()
  })

  it('prompts for an explicit project name instead of inferring one for a non-local folder', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'non_local' }
      }

      if (method === 'projects.for_cwd') {
        return { branch: '', cwd: '/workspace/repo', project: null }
      }

      return {}
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await openFolderAsProject('../repo')

    expect(request).not.toHaveBeenCalledWith('projects.create', expect.anything())
    expect($projectDialog.get()).toMatchObject({ mode: 'open-folder', path: '../repo' })
    expect($startWorkSessionRequest.get()).toBeNull()
  })

  it('uses projects.for_cwd canonical cwd for an existing project', async () => {
    const existing = { id: 'p_existing', name: 'Existing', primary_path: '/workspace/repo' }

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'non_local' }
      }

      if (method === 'projects.for_cwd') {
        return { branch: '', cwd: '/workspace/repo', project: existing }
      }

      return { active_id: existing.id, projects: [], scoped_session_ids: [] }
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await openFolderAsProject('../repo')

    expect($projectScope.get()).toBe(existing.id)
    expect($startWorkSessionRequest.get()).toMatchObject({ path: '/workspace/repo' })
    expect(request).not.toHaveBeenCalledWith('projects.create', expect.anything())
  })

  it('never launches a raw workspace when non-local canonicalization fails', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'non_local' }
      }

      if (method === 'projects.for_cwd') {
        throw new Error('cannot resolve ../missing')
      }

      return {}
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await expect(openFolderAsProject('../missing')).rejects.toThrow('cannot resolve ../missing')

    expect($startWorkSessionRequest.get()).toBeNull()
    expect(request).not.toHaveBeenCalledWith('projects.create', expect.anything())
  })

  it('keeps the local raw-workspace fallback when canonicalization fails', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'local' }
      }

      if (method === 'projects.for_cwd') {
        throw new Error('legacy local failure')
      }

      return {}
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await openFolderAsProject('/local/repo')

    expect($startWorkSessionRequest.get()).toMatchObject({ path: '/local/repo' })
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'warning' }))
  })

  it('does not use the local raw fallback when the profile switches before for-cwd rejects', async () => {
    const forCwd = deferred<never>()

    const gatewayA = {
      connectionState: 'open',
      request: vi.fn((method: string) =>
        method === 'projects.capabilities' ? Promise.resolve({ filesystem_scope: 'local' }) : forCwd.promise
      )
    }

    const gatewayB = { connectionState: 'open', request: vi.fn() }
    let current = gatewayA
    activeGateway.mockImplementation(() => current as never)

    const pending = openFolderAsProject('/profile-a/repo')
    await vi.waitFor(() => expect(gatewayA.request).toHaveBeenCalledWith('projects.for_cwd', expect.anything()))
    current = gatewayB
    $activeGatewayProfile.set('profile-b')
    forCwd.reject(new Error('profile A lookup failed'))

    await expect(pending).rejects.toThrow('Active Hermes profile changed')
    expect($startWorkSessionRequest.get()).toBeNull()
  })

  it('does not use the local raw fallback when the profile switches before create rejects', async () => {
    const create = deferred<never>()

    const gatewayA = {
      connectionState: 'open',
      request: vi.fn((method: string) => {
        if (method === 'projects.capabilities') {
          return Promise.resolve({ filesystem_scope: 'local' })
        }

        if (method === 'projects.for_cwd') {
          return Promise.resolve({ branch: '', cwd: '/canonical/profile-a/repo', project: null })
        }

        if (method === 'projects.create') {
          return create.promise
        }

        return Promise.resolve({})
      })
    }

    const gatewayB = { connectionState: 'open', request: vi.fn() }
    let current = gatewayA
    activeGateway.mockImplementation(() => current as never)

    const pending = openFolderAsProject('/profile-a/repo')
    await vi.waitFor(() => expect(gatewayA.request).toHaveBeenCalledWith('projects.create', expect.anything()))
    current = gatewayB
    $activeGatewayProfile.set('profile-b')
    create.reject(new Error('profile A create failed'))

    await expect(pending).rejects.toThrow('Active Hermes profile changed')
    expect($startWorkSessionRequest.get()).toBeNull()
  })

  it('keeps auto-project appearance adoption on projects.create and applies canonical response', async () => {
    const created = {
      archived: false,
      board_slug: null,
      color: '#123456',
      created_at: 1,
      description: null,
      folders: [{ added_at: 1, is_primary: true, label: null, path: '/workspace/repo' }],
      icon: null,
      id: 'p_adopted',
      name: 'Repo',
      primary_path: '/workspace/repo',
      slug: 'repo'
    }

    const request = vi.fn(async (method: string) =>
      method === 'projects.create'
        ? { project: created }
        : { active_id: null, projects: [created], scoped_session_ids: [] }
    )

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await expect(
      setProjectAppearance(
        { color: null, icon: null, id: '../repo', isAuto: true, label: 'Repo', path: '../repo' },
        { color: '#123456' }
      )
    ).resolves.toBe(true)

    expect(request).toHaveBeenCalledWith(
      'projects.create',
      expect.objectContaining({ folders: ['../repo'], primary_path: '../repo' })
    )
    expect($projects.get()).toContainEqual(
      expect.objectContaining({ id: 'p_adopted', primary_path: '/workspace/repo' })
    )
  })

  it('uses the local picker for no-path open and then canonicalizes the selection', async () => {
    const existing = { id: 'p_local', name: 'Local', primary_path: '/canonical/repo' }
    selectDesktopPaths.mockResolvedValue(['/picked/repo'])

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'local' }
      }

      if (method === 'projects.for_cwd') {
        return { branch: '', cwd: '/canonical/repo', project: existing }
      }

      return { active_id: existing.id, projects: [], scoped_session_ids: [] }
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await openFolderAsProject()

    expect(selectDesktopPaths).toHaveBeenCalledOnce()
    expect(request).toHaveBeenCalledWith('projects.for_cwd', { cwd: '/picked/repo', profile: 'default' })
    expect($startWorkSessionRequest.get()).toMatchObject({ path: '/canonical/repo' })
  })

  it('rejects a capability result after the captured profile changes', async () => {
    const capability = deferred<{ filesystem_scope: 'local' }>()
    const request = vi.fn(() => capability.promise)
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    const context = captureProjectPathContext()!
    const pending = projectFilesystemScope(context)

    $activeGatewayProfile.set('other')
    capability.resolve({ filesystem_scope: 'local' })

    await expect(pending).rejects.toThrow('Active Hermes profile changed')
    expect(selectDesktopPaths).not.toHaveBeenCalled()
  })

  it('discards a projects.for_cwd response after the captured profile changes', async () => {
    const forCwd = deferred<{
      branch: string
      cwd: string
      project: { id: string; name: string; primary_path: string }
    }>()

    const request = vi.fn((method: string) => {
      if (method === 'projects.capabilities') {
        return Promise.resolve({ filesystem_scope: 'non_local' })
      }

      return forCwd.promise
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    const pending = openFolderAsProject('../repo')

    await vi.waitFor(() => expect(request).toHaveBeenCalledWith('projects.for_cwd', expect.anything()))
    $activeGatewayProfile.set('other')
    forCwd.resolve({
      branch: '',
      cwd: '/workspace/repo',
      project: { id: 'p_a', name: 'A', primary_path: '/workspace/repo' }
    })

    await expect(pending).rejects.toThrow('Active Hermes profile changed')
    expect($projectScope.get()).toBe(ALL_PROJECTS)
    expect($startWorkSessionRequest.get()).toBeNull()
  })

  it('does not write IDEA.md through the host filesystem for a non-local project path', async () => {
    const created = {
      archived: false,
      board_slug: null,
      color: null,
      created_at: 1,
      description: null,
      folders: [{ added_at: 1, is_primary: true, label: null, path: '/workspace/repo' }],
      icon: null,
      id: 'p_remote',
      name: 'Remote',
      primary_path: '/workspace/repo',
      slug: 'remote'
    }

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'non_local' }
      }

      if (method === 'projects.create') {
        return { project: created }
      }

      return { active_id: created.id, projects: [created], scoped_session_ids: [] }
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    const context = captureProjectPathContext()!

    await createProject({
      context,
      folders: ['../repo'],
      idea: 'Build the remote project',
      name: 'Remote',
      primaryPath: '../repo'
    })
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith('projects.capabilities', { profile: 'default' }))

    expect(writeDesktopFileText).not.toHaveBeenCalled()
  })

  it('binds a local-filesystem IDEA.md write to the host route captured with the project dialog', async () => {
    const created = {
      folders: [{ added_at: 1, is_primary: true, label: null, path: '/host-a/repo' }],
      id: 'p_local',
      name: 'Local',
      primary_path: '/host-a/repo'
    }

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.capabilities') {
        return { filesystem_scope: 'local' }
      }

      if (method === 'projects.create') {
        return { project: created }
      }

      return { active_id: created.id, projects: [created], scoped_session_ids: [] }
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
    isDesktopFsRemoteMode.mockReturnValue(true)
    const context = captureProjectPathContext()!

    await createProject({ context, folders: ['/host-a/repo'], idea: 'Pinned idea', name: 'Local' })
    await vi.waitFor(() => expect(writeDesktopFileText).toHaveBeenCalledOnce())

    expect(writeDesktopFileText).toHaveBeenCalledWith('/host-a/repo/IDEA.md', 'Pinned idea\n', {
      connectionId: 'host-a',
      profile: 'default',
      remote: true
    })
  })

  it('closes stale project/worktree dialogs and cancels the picker on a gateway generation switch', () => {
    $projectDialog.set({ mode: 'create' })
    $worktreeDialog.set({ repoPath: '/old/repo' })
    vi.mocked(fs.cancelDesktopFsRemotePicker).mockClear()

    gatewayGenerationAtom.set(gatewayGenerationAtom.get() + 1)

    expect($projectDialog.get()).toBeNull()
    expect($worktreeDialog.get()).toBeNull()
    expect(fs.cancelDesktopFsRemotePicker).toHaveBeenCalledOnce()
  })

  it('does not refresh the new profile after a captured delete succeeds on the old profile', async () => {
    const write = deferred<{ active_id: null; projects: [] }>()

    const request = vi.fn((method: string) =>
      method === 'projects.delete' ? write.promise : Promise.resolve({ active_id: null, projects: [] })
    )

    const gateway = { connectionState: 'open', request }
    activeGateway.mockReturnValue(gateway as never)
    gatewayAtom.set(gateway as never)
    $activeGatewayProfile.set('profile-a')
    const context = captureProjectPathContext()!
    $projects.set([{ folders: [], id: 'p_a', name: 'Original' } as never])

    const pending = deleteProject('p_a', context)
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith('projects.delete', expect.anything()))
    $activeGatewayProfile.set('profile-b')
    request.mockClear()
    write.resolve({ active_id: null, projects: [] })
    await pending

    expect(request).not.toHaveBeenCalled()
  })

  it.each([
    ['rename', (context: ReturnType<typeof captureProjectPathContext>) => renameProject('p_a', 'Renamed', context!)],
    ['delete', (context: ReturnType<typeof captureProjectPathContext>) => deleteProject('p_a', context!)]
  ] as const)('keeps a stale %s failure from rolling profile A state into profile B', async (_kind, mutate) => {
    const write = deferred<never>()
    const requestA = vi.fn(() => write.promise)
    const gatewayA = { connectionState: 'open', request: requestA }
    activeGateway.mockReturnValue(gatewayA as never)
    $activeGatewayProfile.set('profile-a')
    const context = captureProjectPathContext()!
    $projects.set([{ folders: [], id: 'p_a', name: 'Original' } as never])
    $projectTree.set([{ groups: [], id: 'p_a', label: 'Original', path: '/a', repos: [], sessionCount: 0 } as never])

    const pending = mutate(context)
    await vi.waitFor(() => expect(requestA).toHaveBeenCalled())

    $activeGatewayProfile.set('profile-b')
    $projects.set([{ folders: [], id: 'p_b', name: 'Profile B' } as never])
    $projectTree.set([{ groups: [], id: 'p_b', label: 'Profile B', path: '/b', repos: [], sessionCount: 0 } as never])
    write.reject(new Error('profile A write failed'))

    await expect(pending).rejects.toThrow('profile A write failed')
    expect($projects.get().map(project => project.id)).toEqual(['p_b'])
    expect($projectTree.get().map(project => project.id)).toEqual(['p_b'])
    expect(requestA).toHaveBeenCalledWith(
      expect.stringMatching(/^projects\.(update|delete)$/),
      expect.objectContaining({ profile: 'profile-a' })
    )
  })
})
