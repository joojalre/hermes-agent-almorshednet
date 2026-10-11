import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { $apiRequestScope } from '@/api/client'
import * as api from '@/hermes'
import type { StatusResponse } from '@/hermes'

import { CommandCenterView } from './index'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof api>()),
  getActionStatus: vi.fn(() => Promise.resolve({ running: false })),
  getLogs: vi.fn(() => Promise.resolve({ lines: [] })),
  getStatus: vi.fn(),
  getUsageAnalytics: vi.fn(),
  restartGateway: vi.fn(),
  updateHermes: vi.fn()
}))
vi.mock('@/lib/session-export', () => ({ exportSession: vi.fn() }))
vi.mock('./maintenance', () => ({ MaintenancePanel: () => null }))

const status = (overrides: Partial<StatusResponse> = {}) =>
  ({
    version: 'git.1fd1827',
    active_sessions: 0,
    gateway_running: true,
    gateway_state: 'running',
    gateway_heartbeat_stale_s: null,
    ...overrides
  }) as StatusResponse

function open() {
  render(
    <MemoryRouter>
      <CommandCenterView
        initialSection="system"
        onClose={() => {}}
        onDeleteSession={async () => {}}
        onOpenSession={() => {}}
      />
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  $apiRequestScope.set({ profile: 'A', connectionId: 'local' })
  vi.mocked(api.getLogs).mockResolvedValue({ file: 'agent', lines: [] })
  vi.mocked(api.getStatus).mockResolvedValue(status())
})
afterEach(() => {
  cleanup()
  $apiRequestScope.set({ profile: null, connectionId: null })
})

it('uses fresh status even when fetching logs fails', async () => {
  vi.mocked(api.getLogs).mockRejectedValue(new Error('logs unavailable'))
  open()
  expect(await screen.findByText('Messaging gateway running')).toBeTruthy()
  expect(screen.getByText(/git\.1fd1827/)).toBeTruthy()
})

it('shows stale heartbeat as needing attention even with a live process', async () => {
  vi.mocked(api.getStatus).mockResolvedValue(status({ gateway_heartbeat_stale_s: 900 }))
  open()
  expect(await screen.findByText('Messaging gateway needs attention')).toBeTruthy()
  expect(screen.queryByText('Messaging gateway running')).toBeNull()
})

it('does not keep a running badge when the next profile status fails', async () => {
  open()
  await screen.findByText('Messaging gateway running')
  vi.mocked(api.getStatus).mockRejectedValue(new Error('status unavailable'))
  act(() => $apiRequestScope.setKey('profile', 'B'))
  await screen.findByText('status unavailable')
  expect(screen.queryByText('Messaging gateway running')).toBeNull()
})

it('ignores the old A reply after changing A to B to A', async () => {
  let resolveOld!: (value: StatusResponse) => void
  vi.mocked(api.getStatus).mockReturnValueOnce(
    new Promise(resolve => {
      resolveOld = resolve
    })
  )
  open()
  await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(1))
  act(() => $apiRequestScope.setKey('profile', 'B'))
  await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(2))
  act(() => $apiRequestScope.setKey('profile', 'A'))
  await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(3))
  await screen.findByText('Messaging gateway running')
  await act(async () => resolveOld(status({ gateway_running: false, gateway_state: 'stopped' })))
  expect(screen.getByText('Messaging gateway running')).toBeTruthy()
  expect(screen.queryByText('Messaging gateway stopped')).toBeNull()
})

it('ignores an action accepted after its profile was left and revisited', async () => {
  let accepted!: (value: api.ActionResponse) => void
  vi.mocked(api.updateHermes).mockReturnValueOnce(
    new Promise(resolve => {
      accepted = resolve
    })
  )
  open()
  await screen.findByText('Messaging gateway running')
  fireEvent.click(screen.getByRole('button', { name: 'Update Hermes' }))
  await waitFor(() => expect(api.updateHermes).toHaveBeenCalledOnce())
  act(() => $apiRequestScope.setKey('profile', 'B'))
  await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(2))
  act(() => $apiRequestScope.setKey('profile', 'A'))
  await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(3))
  await act(async () => accepted({ ok: true, name: 'hermes-update', pid: 123 }))
  expect(screen.queryByText('An update is currently installing.')).toBeNull()
})

it('continues checking when a replacement backend has no completion receipt yet', async () => {
  vi.mocked(api.updateHermes).mockResolvedValue({ ok: true, name: 'hermes-update', pid: 123 })
  vi.mocked(api.getActionStatus)
    .mockResolvedValueOnce({ name: 'hermes-update', pid: null, running: false, exit_code: null, lines: [] })
    .mockResolvedValue({ name: 'hermes-update', pid: null, running: false, exit_code: 0, lines: [] })
  open()
  await screen.findByText('Messaging gateway running')
  fireEvent.click(screen.getByRole('button', { name: 'Update Hermes' }))
  await waitFor(() => expect(api.getActionStatus).toHaveBeenCalledTimes(2), { timeout: 5000 })
  expect(screen.queryByText('An update is currently installing.')).toBeNull()
})
