// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'

import { TOOLSETS_QUERY_KEY, useToolsetsQuery } from '../toolsets/toolsets-data'

import { SKILLS_QUERY_KEY, useSkillsQuery } from './skills-data'

const api = vi.hoisted(() => ({ getSkills: vi.fn(), getToolsets: vi.fn() }))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getSkills: api.getSkills,
  getToolsets: api.getToolsets
}))

let client: QueryClient

afterEach(() => {
  cleanup()
  client?.clear()
  vi.resetAllMocks()
})

it('surfaces the first scoped list failure and recovers through manual Refresh', async () => {
  const profile = { connectionId: 'local', profile: 'researcher' }
  const failure = new Error('Timed out connecting to Hermes backend after 30000ms')
  api.getSkills.mockRejectedValue(failure)
  api.getToolsets.mockRejectedValue(failure)
  // Keep React Query's real defaults: an inherited retry policy must not
  // postpone the page's existing error and manual Refresh affordance.
  client = new QueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const { result } = renderHook(() => ({ skills: useSkillsQuery(profile), toolsets: useToolsetsQuery(profile) }), {
    wrapper
  })

  await waitFor(() => expect([result.current.skills.isError, result.current.toolsets.isError]).toEqual([true, true]), {
    timeout: 2000
  })
  expect([result.current.skills.isPending, result.current.toolsets.isPending]).toEqual([false, false])
  expect(result.current.skills.error).toBe(failure)
  expect(result.current.toolsets.error).toBe(failure)
  expect(api.getSkills).toHaveBeenCalledTimes(1)
  expect(api.getToolsets).toHaveBeenCalledTimes(1)

  api.getSkills.mockResolvedValue([])
  api.getToolsets.mockResolvedValue([])
  // Match the page's Refresh action, which invalidates both list prefixes.
  await act(async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: SKILLS_QUERY_KEY }),
      client.invalidateQueries({ queryKey: TOOLSETS_QUERY_KEY })
    ])
  })
  await waitFor(() =>
    expect([result.current.skills.isSuccess, result.current.toolsets.isSuccess]).toEqual([true, true])
  )
  expect(result.current.skills.data).toEqual([])
  expect(result.current.toolsets.data).toEqual([])
  expect(api.getSkills.mock.calls).toEqual([[profile], [profile]])
  expect(api.getToolsets.mock.calls).toEqual([[profile], [profile]])
})
