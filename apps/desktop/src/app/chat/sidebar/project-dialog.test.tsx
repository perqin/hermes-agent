import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProjectDialog } from './project-dialog'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel', save: 'Save' },
      sidebar: {
        projects: {
          addFolder: 'Add folder',
          create: 'Create',
          createDesc: 'Create a new project',
          createFailed: 'Failed to create project',
          createTitle: 'New project',
          foldersLabel: 'Folders',
          pathAdd: 'Add path',
          pathError: 'The folder could not be resolved',
          pathHelp: 'Enter a path in the profile terminal filesystem.',
          pathLabel: 'Project folder path',
          pathLoading: 'Checking where project folders are stored…',
          pathPlaceholder: '~/path/to/project',
          openFolderTitle: 'Open folder as project',
          ideaGenerate: 'Generate',
          ideaGenerating: 'Generating…',
          ideaLabel: 'Idea',
          ideaPlaceholder: 'What are you building?',
          ideaShuffle: 'Shuffle ideas',
          namePlaceholder: 'Project name',
          noFolders: 'No folders yet',
          primaryBadge: 'Primary',
          removeFolder: 'Remove folder'
        }
      }
    }
  })
}))

// $projectDialog is a real nanostore atom in the app; recreate it here so
// useStore behaves identically without pulling in the rest of the projects
// store (backend calls, project list, etc.) which is irrelevant to the Tip fix.
// vi.mock factories are hoisted above the rest of the file, so the atom must
// be created inside vi.hoisted to exist by the time the factory runs.
const { $newProjectDropPlacement, $projectDialog } = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores

  return {
    // Where a "New project" DRAG armed its drop (null = plain click).
    $newProjectDropPlacement: atom<{ anchor: string; before?: null | string; dir: string } | null>(null),
    $projectDialog: atom<{
      context?: Record<string, unknown>
      mode: 'create' | 'rename' | 'add-folder' | 'open-folder'
      name?: string
      projectId?: string
    } | null>({ mode: 'create', context: { generation: 1, profile: 'default', remoteConnection: false } })
  }
})

vi.mock('@/store/projects', () => ({
  $newProjectDropPlacement,
  $projectDialog,
  addProjectFolder: vi.fn(),
  clearNewProjectDropPlacement: vi.fn(),
  closeProjectDialog: vi.fn(),
  createProject: vi.fn(),
  generateProjectIdea: vi.fn(),
  openFolderAsProject: vi.fn(),
  pickProjectFolder: vi.fn(async () => '/Users/test/my-folder'),
  projectFilesystemScope: vi.fn(async () => 'local'),
  renameProject: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notifyError: vi.fn()
}))

vi.mock('@/lib/project-idea-templates', () => ({
  randomIdeaTemplates: () => [{ emoji: '🚀', idea: 'A rocket tracker', label: 'Rocket tracker' }]
}))

const tipTrigger = (el: HTMLElement) => el.closest('[data-slot="tooltip-trigger"]')

// Fill the create form and click Create once the form is actually submittable
// (creation requires a name + at least one folder, so the button stays
// disabled until both are in). Awaiting the enable also keeps an async submit
// from one test leaking into the next.
async function fillCreateForm() {
  fireEvent.change(screen.getByPlaceholderText('Project name'), { target: { value: 'Skunkworks' } })
  fireEvent.click(await screen.findByRole('button', { name: 'Add folder' }))
  await screen.findByText('/Users/test/my-folder')

  const create = screen.getByRole('button', { name: 'Create' }) as HTMLButtonElement

  await waitFor(() => expect(create.disabled).toBe(false))
  fireEvent.click(create)
}

describe('ProjectDialog', () => {
  beforeEach(async () => {
    const store = vi.mocked(await import('@/store/projects'))

    store.projectFilesystemScope.mockResolvedValue('local')
  })

  afterEach(() => {
    $projectDialog.set({ mode: 'create', context: { generation: 1, profile: 'default', remoteConnection: false } })
    vi.clearAllMocks()
  })

  it('wraps the "shuffle idea" button in a Tip', () => {
    render(<ProjectDialog />)

    const button = screen.getByRole('button', { name: 'Shuffle ideas' })
    expect(tipTrigger(button)).toBeTruthy()
  })

  it('wraps the "remove folder" button in a Tip once a folder is added', async () => {
    render(<ProjectDialog />)

    fireEvent.click(await screen.findByRole('button', { name: 'Add folder' }))

    const button = await screen.findByRole('button', { name: 'Remove folder' })
    expect(tipTrigger(button)).toBeTruthy()
  })

  it('forwards an armed drag placement to createProject on submit', async () => {
    const { clearNewProjectDropPlacement, createProject } = vi.mocked(await import('@/store/projects'))
    const placement = { anchor: 'workspace', dir: 'center' }

    $newProjectDropPlacement.set(placement)
    render(<ProjectDialog />)
    await fillCreateForm()
    await waitFor(() => expect(createProject).toHaveBeenCalledOnce())
    expect(createProject).toHaveBeenCalledTimes(1)

    expect(createProject.mock.calls[0]?.[0]).toMatchObject({ dropPlacement: placement })

    // Closing the dialog clears the store's arm so no later create inherits it.
    // The clear rides the post-close effect, so wait for it to flush.
    await waitFor(() => expect(clearNewProjectDropPlacement).toHaveBeenCalled())
  })

  it('keeps the armed placement when the create FAILS, so a retry still lands where dropped', async () => {
    const { clearNewProjectDropPlacement, createProject } = vi.mocked(await import('@/store/projects'))
    const placement = { anchor: 'workspace', dir: 'right' }

    vi.mocked(createProject).mockClear()
    vi.mocked(clearNewProjectDropPlacement).mockClear()
    vi.mocked(createProject).mockRejectedValueOnce(new Error('gateway hiccup'))

    $newProjectDropPlacement.set(placement)
    render(<ProjectDialog />)
    await fillCreateForm()
    await waitFor(() => expect(createProject).toHaveBeenCalledOnce())

    // The failed attempt consumed nothing and closed nothing — the dialog
    // stays open for a retry with the placement intact.
    expect(clearNewProjectDropPlacement).not.toHaveBeenCalled()
    expect(createProject.mock.calls[0]?.[0]).toMatchObject({ dropPlacement: placement })

    // Retry succeeds → forwards the SAME placement.
    await fillCreateForm()
    await waitFor(() => expect(createProject).toHaveBeenCalledTimes(2))
    expect(createProject.mock.calls[1]?.[0]).toMatchObject({ dropPlacement: placement })
  })

  it('sends no placement when opened by a plain click', async () => {
    const { createProject } = vi.mocked(await import('@/store/projects'))

    vi.mocked(createProject).mockClear()
    $newProjectDropPlacement.set(null)
    render(<ProjectDialog />)
    await fillCreateForm()
    await waitFor(() => expect(createProject).toHaveBeenCalledOnce())

    expect(createProject.mock.calls[0]?.[0]).toMatchObject({ dropPlacement: undefined })
  })

  it('uses raw text entry for a non-local profile and never invokes a picker', async () => {
    const store = vi.mocked(await import('@/store/projects'))

    store.projectFilesystemScope.mockResolvedValueOnce('non_local')
    render(<ProjectDialog />)

    const path = await screen.findByRole('textbox', { name: 'Project folder path' })
    fireEvent.change(screen.getByPlaceholderText('Project name'), { target: { value: 'Remote repo' } })
    fireEvent.change(path, { target: { value: '../repo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add path' }))
    await screen.findByText('../repo')
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(store.createProject).toHaveBeenCalledOnce())
    expect(store.createProject.mock.calls[0]?.[0]).toMatchObject({ folders: ['../repo'], name: 'Remote repo' })
    expect(store.pickProjectFolder).not.toHaveBeenCalled()
  })

  it('keeps non-local add-folder input and backend error visible for correction', async () => {
    const store = vi.mocked(await import('@/store/projects'))

    store.projectFilesystemScope.mockResolvedValueOnce('non_local')
    store.addProjectFolder.mockRejectedValueOnce(new Error('cannot resolve ~/missing'))
    $projectDialog.set({
      context: { generation: 1, profile: 'remote', remoteConnection: false },
      mode: 'add-folder',
      projectId: 'p_remote'
    })
    render(<ProjectDialog />)

    const path = await screen.findByRole('textbox', { name: 'Project folder path' })
    fireEvent.change(path, { target: { value: '~/missing' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add path' }))

    expect(await screen.findByText('cannot resolve ~/missing')).toBeTruthy()
    expect((path as HTMLInputElement).value).toBe('~/missing')
    expect(store.addProjectFolder).toHaveBeenCalledOnce()
    expect(store.closeProjectDialog).not.toHaveBeenCalled()
    expect(store.pickProjectFolder).not.toHaveBeenCalled()
  })

  it('prevents duplicate non-local submissions while validation is pending', async () => {
    const store = vi.mocked(await import('@/store/projects'))
    let finish!: () => void

    store.projectFilesystemScope.mockResolvedValueOnce('non_local')
    store.addProjectFolder.mockImplementationOnce(() => new Promise<void>(resolve => (finish = resolve)))
    $projectDialog.set({
      context: { generation: 1, profile: 'remote', remoteConnection: false },
      mode: 'add-folder',
      projectId: 'p_remote'
    })
    render(<ProjectDialog />)

    const path = await screen.findByRole('textbox', { name: 'Project folder path' })
    fireEvent.change(path, { target: { value: '/workspace/repo' } })
    const add = screen.getByRole('button', { name: 'Add path' })
    fireEvent.click(add)
    fireEvent.click(add)

    expect(store.addProjectFolder).toHaveBeenCalledOnce()
    finish()
    await waitFor(() => expect(store.closeProjectDialog).toHaveBeenCalled())
  })

  it('submits the reusable open-folder text prompt without changing the raw path', async () => {
    const store = vi.mocked(await import('@/store/projects'))
    const context = { generation: 1, profile: 'remote', remoteConnection: false }

    store.projectFilesystemScope.mockResolvedValueOnce('unknown')
    $projectDialog.set({ context, mode: 'open-folder' })
    render(<ProjectDialog />)

    const path = await screen.findByRole('textbox', { name: 'Project folder path' })
    fireEvent.change(path, { target: { value: '~/work/repo' } })
    fireEvent.keyDown(path, { key: 'Enter' })

    await waitFor(() => expect(store.openFolderAsProject).toHaveBeenCalledOnce())
    expect(store.openFolderAsProject).toHaveBeenCalledWith('~/work/repo', context)
  })
})
