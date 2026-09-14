/// <reference types="node" />

import { Buffer } from 'node:buffer'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesReadDirEntry, HermesReadDirResult } from '@/global'
import { setProjectFilesystemScope } from '@/lib/project-filesystem-capability'
import { $connection } from '@/store/session'

import { clearProjectDirCache, readProjectDir } from './ipc'

const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()
const readFileDataUrl = vi.fn<(path: string) => Promise<string>>()
const gitRoot = vi.fn<(path: string) => Promise<string | null>>()

function ok(entries: HermesReadDirEntry[]): HermesReadDirResult {
  return { entries }
}

function dataUrl(text: string) {
  return `data:text/plain;base64,${Buffer.from(text, 'utf8').toString('base64')}`
}

function installBridge() {
  ;(
    window as unknown as {
      hermesDesktop: {
        gitRoot: typeof gitRoot
        readDir: typeof readDir
        readFileDataUrl: typeof readFileDataUrl
      }
    }
  ).hermesDesktop = { gitRoot, readDir, readFileDataUrl }
}

describe('readProjectDir', () => {
  beforeEach(() => {
    setProjectFilesystemScope('local')
    clearProjectDirCache()
    readDir.mockReset()
    readFileDataUrl.mockReset()
    gitRoot.mockReset()
    installBridge()
  })

  afterEach(() => {
    $connection.set(null)
    setProjectFilesystemScope('unknown')
    clearProjectDirCache()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('returns no-bridge when the desktop bridge is unavailable', async () => {
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop

    await expect(readProjectDir('/repo')).resolves.toEqual({ entries: [], error: 'no-bridge' })
  })

  it.each(['non_local', 'unknown'] as const)('fails closed before filesystem or git IPC for %s scope', async scope => {
    setProjectFilesystemScope(scope)

    await expect(readProjectDir('/backend/repo')).resolves.toEqual({ entries: [], error: 'non-local-filesystem' })
    expect(readDir).not.toHaveBeenCalled()
    expect(gitRoot).not.toHaveBeenCalled()
    expect(readFileDataUrl).not.toHaveBeenCalled()
  })

  it('stops before git discovery when capability changes during the directory read', async () => {
    readDir.mockImplementation(async () => {
      setProjectFilesystemScope('unknown')

      return ok([{ name: 'a', path: '/old/a', isDirectory: false }])
    })
    await expect(readProjectDir('/old')).resolves.toEqual({ entries: [], error: 'non-local-filesystem' })
    expect(gitRoot).not.toHaveBeenCalled()
  })

  it.each(['directory', 'git-root', 'ignore-listing', 'ignore-content'])(
    'abandons the old route after the %s await, even on a profile round trip',
    async boundary => {
      const invalidate = () => {
        $connection.set({ mode: 'local', profile: 'other' } as never)
        $connection.set(null)
      }

      let reads = 0
      readDir.mockImplementation(async () => {
        reads += 1

        if ((boundary === 'directory' && reads === 1) || (boundary === 'ignore-listing' && reads === 2)) {
          invalidate()
        }

        return ok([{ name: '.gitignore', path: '/old/.gitignore', isDirectory: false }])
      })
      gitRoot.mockImplementation(async () => {
        if (boundary === 'git-root') {
          invalidate()
        }

        return '/old'
      })
      readFileDataUrl.mockImplementation(async () => {
        if (boundary === 'ignore-content') {
          invalidate()
        }

        return dataUrl('')
      })
      await expect(readProjectDir('/old')).resolves.toEqual({ entries: [], error: 'non-local-filesystem' })

      if (boundary === 'directory') {
        expect(gitRoot).not.toHaveBeenCalled()
      }

      if (boundary === 'git-root') {
        expect(readDir).toHaveBeenCalledTimes(1)
      }

      if (boundary === 'ignore-listing') {
        expect(readFileDataUrl).not.toHaveBeenCalled()
      }
    }
  )

  it('filters gitignored entries when readDir returns Windows-style paths', async () => {
    gitRoot.mockResolvedValue('C:\\repo')
    readDir.mockImplementation(async path => {
      if (path === 'C:\\repo\\src') {
        return ok([
          { name: 'debug.log', path: 'C:\\repo\\src\\debug.log', isDirectory: false },
          { name: '临时.txt', path: 'C:\\repo\\src\\临时.txt', isDirectory: false },
          { name: 'keep.ts', path: 'C:\\repo\\src\\keep.ts', isDirectory: false }
        ])
      }

      if (path === 'C:\\repo') {
        return ok([{ name: '.gitignore', path: 'C:\\repo\\.gitignore', isDirectory: false }])
      }

      if (path === 'C:\\repo\\src') {
        return ok([])
      }

      return ok([])
    })
    readFileDataUrl.mockResolvedValue(dataUrl('# Unicode 路径规则\nsrc/*.log\nsrc/临时.txt\n'))

    const result = await readProjectDir('C:\\repo\\src', 'C:\\repo')

    expect(result.entries.map(entry => entry.name)).toEqual(['keep.ts'])
    expect(gitRoot).toHaveBeenCalledWith('C:\\repo')
    expect(readFileDataUrl).toHaveBeenCalledWith('C:\\repo\\.gitignore')
  })

  it('filters gitignored entries when Windows path casing differs across IPC results', async () => {
    gitRoot.mockResolvedValue('C:\\Repo')
    readDir.mockImplementation(async path => {
      if (path === 'c:\\repo\\src') {
        return ok([
          { name: 'debug.log', path: 'c:\\repo\\src\\debug.log', isDirectory: false },
          { name: 'keep.ts', path: 'c:\\repo\\src\\keep.ts', isDirectory: false }
        ])
      }

      if (path === 'C:\\Repo') {
        return ok([{ name: '.gitignore', path: 'C:\\Repo\\.gitignore', isDirectory: false }])
      }

      if (path === 'C:\\Repo\\src') {
        return ok([])
      }

      return ok([])
    })
    readFileDataUrl.mockResolvedValue(dataUrl('src/*.log\n'))

    const result = await readProjectDir('c:\\repo\\src', 'c:\\repo')

    expect(result.entries.map(entry => entry.name)).toEqual(['keep.ts'])
  })

  it('does not fetch .gitignore contents when listings do not contain .gitignore', async () => {
    gitRoot.mockResolvedValue('/repo')
    readDir.mockImplementation(async path => {
      if (path === '/repo/src') {
        return ok([{ name: 'debug.log', path: '/repo/src/debug.log', isDirectory: false }])
      }

      return ok([])
    })

    const result = await readProjectDir('/repo/src', '/repo')

    expect(result.entries.map(entry => entry.name)).toEqual(['debug.log'])
    expect(readFileDataUrl).not.toHaveBeenCalled()
  })
})
