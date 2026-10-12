import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { OverlayView } from '@/app/overlays/overlay-view'
import { ConfirmHost } from '@/components/confirm-host'
import type { HermesApiRequest } from '@/global'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { openPreview } from '@/store/preview'

import { MaintenancePanel } from './maintenance'

vi.mock('@/store/preview', () => ({ openPreview: vi.fn() }))

let directory: string
let readsFail = false
let resetCalls: HermesApiRequest[]

const fileFor = (profile: string, file = 'MEMORY.md') => path.join(directory, `${profile}-${file}`)

beforeEach(async () => {
  directory = await mkdtemp(path.join(tmpdir(), 'hermes-maintenance-memory-'))
  await writeFile(fileFor('alpha'), 'alpha memory fixture')
  await writeFile(fileFor('beta'), 'beta memory fixture has a different size')
  await writeFile(fileFor('alpha', 'USER.md'), 'alpha user fixture')
  await writeFile(fileFor('beta', 'USER.md'), 'beta user fixture')
  readsFail = false
  resetCalls = []
  vi.mocked(openPreview).mockClear()
  setApiRequestConnection(null)
  setApiRequestProfile('alpha')
  vi.stubGlobal('hermesDesktop', {
    api: async (request: HermesApiRequest) => {
      const profile = request.profile || 'alpha'

      if (request.path === '/api/memory') {
        if (readsFail) {
          throw new Error('Memory status temporarily unavailable')
        }

        return {
          active: '',
          providers: [],
          builtin_files: {
            memory: (await stat(fileFor(profile))).size,
            user: (await stat(fileFor(profile, 'USER.md'))).size
          }
        }
      }

      if (request.path === '/api/status') {
        return { hermes_home: path.join(directory, profile) }
      }

      if (request.path === '/api/curator') {
        return { enabled: false, paused: false }
      }

      if (request.path === '/api/memory/reset') {
        resetCalls.push(request)
        const target = (request.body as { target: string }).target
        await writeFile(fileFor(profile, target === 'user' ? 'USER.md' : 'MEMORY.md'), '')

        return { ok: true, deleted: [] }
      }

      throw new Error(`Unexpected fixture request: ${request.path}`)
    }
  })
})

afterEach(async () => {
  settleConfirm(false)
  cleanup()
  $confirmRequest.set(null)
  setApiRequestProfile(null)
  setApiRequestConnection(null)
  vi.unstubAllGlobals()
  await rm(directory, { force: true, recursive: true })
})

function panel(onOpenFile?: () => void) {
  return render(
    <>
      <OverlayView onClose={() => {}}>
        <MaintenancePanel onOpenFile={onOpenFile} />
      </OverlayView>
      <ConfirmHost />
    </>
  )
}

describe('Maintenance memory actions', () => {
  it.each(['Reset memory', 'Reset profile'])(
    'opens %s confirmation and cancellation preserves both files',
    async label => {
      panel()
      const reset = await screen.findByRole('button', { name: label })
      expect((reset as HTMLButtonElement).disabled).toBe(false)
      fireEvent.click(reset)
      expect(await screen.findByRole('dialog')).toBeTruthy()
      expect(resetCalls).toEqual([])
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      expect(resetCalls).toEqual([])
      expect(await readFile(fileFor('alpha'), 'utf8')).toBe('alpha memory fixture')
      expect(await readFile(fileFor('alpha', 'USER.md'), 'utf8')).toBe('alpha user fixture')
    }
  )

  it('cancels an unanswered reset when the profile changes and loads the new file sizes', async () => {
    panel()
    fireEvent.click(await screen.findByRole('button', { name: 'Reset memory' }))
    await screen.findByRole('dialog')
    await act(async () => setApiRequestProfile('beta'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(await screen.findByText(`${(await stat(fileFor('beta'))).size} B`)).toBeTruthy()
    expect(resetCalls).toEqual([])
    expect(await readFile(fileFor('alpha'), 'utf8')).toBe('alpha memory fixture')
    expect(await readFile(fileFor('beta'), 'utf8')).toBe('beta memory fixture has a different size')
  })

  it('opens each active profile memory file through the existing file preview', async () => {
    const opened = vi.fn()
    panel(opened)
    await screen.findByRole('button', { name: 'Reset memory' })
    await act(async () => setApiRequestProfile('beta'))

    for (const [label, filename] of [
      ['Agent memory (MEMORY.md)', 'MEMORY.md'],
      ['User profile (USER.md)', 'USER.md']
    ] as const) {
      fireEvent.click(await screen.findByRole('button', { name: label }))
      await waitFor(() =>
        expect(openPreview).toHaveBeenLastCalledWith(
          expect.objectContaining({
            kind: 'file',
            path: path.join(directory, 'beta', 'memories', filename),
            language: 'markdown',
            previewKind: 'text'
          })
        )
      )
    }

    expect(opened).toHaveBeenCalledTimes(2)
    expect(resetCalls).toEqual([])
  })

  it('offers a retry after memory status fails instead of leaving a permanent loader', async () => {
    readsFail = true
    panel()
    expect(await screen.findByText('Memory status temporarily unavailable')).toBeTruthy()
    readsFail = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: 'Reset memory' })).toBeTruthy()
  })
})
