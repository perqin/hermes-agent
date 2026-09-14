import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ desktopApi: vi.fn(), hermesApi: vi.fn(), invalidate: vi.fn() }))

vi.mock('./client', () => ({
  capabilityScoped: () => ({}),
  hermesApi: mocks.hermesApi,
  profileScoped: () => ({}),
  STARTUP_REQUEST_TIMEOUT_MS: 1000
}))

vi.mock('@/lib/project-filesystem-capability', () => ({
  projectFilesystemConfigWritten: mocks.invalidate
}))

import { saveHermesConfig, saveHermesConfigRecord } from './config'

describe('config writes invalidate project filesystem capability centrally', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('window', { hermesDesktop: { api: mocks.desktopApi } })
  })

  it('invalidates after successful advanced autosave/import/reset writes that can replace terminal.backend', async () => {
    mocks.hermesApi.mockResolvedValue({ ok: true })
    mocks.desktopApi.mockResolvedValue({ ok: true })

    await saveHermesConfig({ terminal: { backend: 'provider-defined' } })
    await saveHermesConfigRecord({ terminal: { backend: 'another-provider' } })

    expect(mocks.invalidate).toHaveBeenCalledTimes(2)
  })

  it('does not invalidate for failed writes', async () => {
    mocks.hermesApi.mockRejectedValue(new Error('write failed'))

    await expect(saveHermesConfig({ terminal: { backend: 'remote' } })).rejects.toThrow('write failed')
    expect(mocks.invalidate).not.toHaveBeenCalled()
  })
})
