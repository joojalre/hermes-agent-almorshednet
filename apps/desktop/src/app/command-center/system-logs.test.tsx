import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { getLogs } from '@/hermes'

import { CommandCenterView } from './index'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getActionStatus: vi.fn(),
  getLogs: vi.fn(),
  getStatus: vi.fn(() => Promise.resolve({ version: '0.21.0', gateway_running: true, active_sessions: 0 })),
  getUsageAnalytics: vi.fn(),
  restartGateway: vi.fn(),
  updateHermes: vi.fn()
}))
vi.mock('@/lib/session-export', () => ({ exportSession: vi.fn() }))
vi.mock('./maintenance', () => ({ MaintenancePanel: () => null }))

function renderSystem() {
  return render(
    <MemoryRouter>
      <CommandCenterView
        initialSection="system"
        onClose={() => {}}
        onDeleteSession={vi.fn()}
        onOpenSession={() => {}}
      />
    </MemoryRouter>
  )
}

function chooseLogFile(container: HTMLElement, file: string) {
  // ResponsiveTabs names every tab by its id; the wide row and the narrow dropdown both carry it.
  const tab = container.querySelector<HTMLElement>(`[data-tour="tab-${file}"]`)

  expect(tab, `log file tab ${file}`).not.toBeNull()
  fireEvent.click(tab!)
}

describe('Command Center log tail', () => {
  beforeEach(() => {
    vi.mocked(getLogs).mockReset()
  })
  afterEach(cleanup)

  it('does not replace the newly selected file with a late response from the previous file', async () => {
    let resolveOld!: (value: Awaited<ReturnType<typeof getLogs>>) => void
    vi.mocked(getLogs).mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveOld = resolve
        })
    )
    vi.mocked(getLogs).mockResolvedValue({ file: 'gateway', lines: ['new gateway entry'] })
    const { container } = renderSystem()
    await waitFor(() => expect(getLogs).toHaveBeenCalledTimes(1))
    chooseLogFile(container, 'gateway')
    await screen.findByText('new gateway entry')
    await act(async () => resolveOld({ file: 'agent', lines: ['old agent entry'] }))
    expect(screen.queryByText('old agent entry')).toBeNull()
    expect(screen.getByText('new gateway entry')).toBeTruthy()
  })
})
