import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesReadDirResult } from '@/global'
import { $connection } from '@/store/session'

import { $showIgnoredRoots } from './prefs'
import { resetProjectTreeState, useProjectTree } from './use-project-tree'

const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()
const gitRoot = vi.fn<(path: string) => Promise<string | null>>()
const sanitizeWorkspaceCwd = vi.fn(async (cwd: string) => ({ cwd, sanitized: false }))

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

function listing(name = 'current.ts', path = '/same'): HermesReadDirResult {
  return { entries: [{ name, path: `${path}/${name}`, isDirectory: false }] }
}

beforeEach(() => {
  $connection.set(null)
  resetProjectTreeState()
  $showIgnoredRoots.set([])
  readDir.mockReset().mockResolvedValue(listing())
  gitRoot.mockReset().mockResolvedValue(null)
  sanitizeWorkspaceCwd.mockReset().mockImplementation(async cwd => ({ cwd, sanitized: false }))
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { readDir, gitRoot, sanitizeWorkspaceCwd }
  vi.useFakeTimers()
})

afterEach(() => {
  cleanup()
  resetProjectTreeState()
  $connection.set(null)
  $showIgnoredRoots.set([])
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  vi.useRealTimers()
})

describe('file tree stalled native reads', () => {
  it.each(['pending', 'completed'] as const)(
    'starts a bounded replacement read after resetting a mounted %s root in the same scope',
    async phase => {
      const oldRead = deferred<HermesReadDirResult>()
      const replacementRead = deferred<HermesReadDirResult>()
      $connection.set({ mode: 'local', connectionId: 'device', profile: 'full' } as never)

      if (phase === 'pending') {
        readDir.mockImplementationOnce(() => oldRead.promise)
      } else {
        readDir.mockResolvedValueOnce(listing('before-reset.ts'))
      }

      readDir.mockImplementationOnce(() => replacementRead.promise)
      const { result } = renderHook(() => useProjectTree('/same'))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(result.current.rootLoading).toBe(phase === 'pending')

      await act(async () => {
        resetProjectTreeState()
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(result.current.rootLoading).toBe(true)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_001)
      })
      expect(result.current.rootLoading).toBe(false)
      expect(result.current.rootError).toBe('read-timeout')
      expect(readDir).toHaveBeenCalledTimes(2)
      expect(readDir.mock.calls.map(([path]) => path)).toEqual(['/same', '/same'])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(9_000)
      })
      expect(readDir).toHaveBeenCalledTimes(2)

      await act(async () => {
        await result.current.refreshRoot()
      })
      expect(readDir).toHaveBeenCalledTimes(3)
      expect(result.current.rootError).toBeNull()
      expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
      await act(async () => {
        oldRead.resolve(listing('old-generation.ts'))
        replacementRead.resolve(listing('expired-replacement.ts'))
        await vi.advanceTimersByTimeAsync(9_000)
      })
      expect(readDir).toHaveBeenCalledTimes(3)
      expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
      expect(result.current.rootLoading).toBe(false)
    }
  )

  it.each(['readDir', 'gitRoot'] as const)(
    'ends a stalled %s read with manual recovery and ignores its late result',
    async seam => {
      const oldRead = deferred<HermesReadDirResult>()
      const oldGit = deferred<string | null>()

      if (seam === 'readDir') {
        readDir.mockImplementationOnce(() => oldRead.promise)
      } else {
        gitRoot.mockImplementationOnce(() => oldGit.promise)
      }

      const { result } = renderHook(() => useProjectTree('/same'))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_001)
      })

      expect(result.current.rootLoading).toBe(false)
      expect(result.current.rootError).toBe('read-timeout')
      expect(result.current.data).toEqual([])
      expect(sanitizeWorkspaceCwd).not.toHaveBeenCalled()

      // A deadline must not launch another hung IPC every ordinary error-retry interval.
      const readsAtTimeout = readDir.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(9_000)
      })
      expect(readDir.mock.calls.length).toBe(readsAtTimeout)
      expect(result.current.rootError).toBe('read-timeout')

      await act(async () => {
        await result.current.refreshRoot()
      })
      expect(result.current.rootError).toBeNull()
      expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
      const gitReadsAfterRetry = gitRoot.mock.calls.length

      await act(async () => {
        oldRead.resolve(listing('obsolete.ts'))
        oldGit.resolve(null)
      })
      expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
      expect(result.current.rootLoading).toBe(false)
      expect(gitRoot.mock.calls.length).toBe(gitReadsAfterRetry)
    }
  )

  it('keeps a default timeout and late result out of the full profile at the same cwd', async () => {
    const defaultRead = deferred<HermesReadDirResult>()
    readDir.mockImplementationOnce(() => defaultRead.promise)
    $connection.set({ mode: 'local', connectionId: 'device', profile: 'default' } as never)
    const { result } = renderHook(() => useProjectTree('/same'))

    await act(async () => {
      $connection.set({ mode: 'local', connectionId: 'device', profile: 'full' } as never)
    })
    expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_001)
    })
    expect(result.current.rootError).toBeNull()
    expect(result.current.rootLoading).toBe(false)
    await act(async () => {
      defaultRead.resolve(listing('default-only.ts'))
    })
    expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
  })

  it('does not overlap ordinary-error retries while the next native read is stalled', async () => {
    const stalled = deferred<HermesReadDirResult>()
    readDir.mockImplementation(() => stalled.promise)
    readDir.mockResolvedValueOnce({ entries: [], error: 'EACCES' })
    const { result } = renderHook(() => useProjectTree('/same'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.rootError).toBe('EACCES')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_001)
    })
    expect(result.current.rootLoading).toBe(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_000)
    })
    expect(readDir).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_001)
    })
    expect(result.current.rootLoading).toBe(false)
    expect(result.current.rootError).toBe('read-timeout')
    expect(readDir).toHaveBeenCalledTimes(2)
  })

  it.each(['readDir', 'gitRoot'] as const)('ends a child %s stall and allows an explicit retry', async seam => {
    const oldRead = deferred<HermesReadDirResult>()
    const oldGit = deferred<string | null>()
    let childReady = false
    readDir.mockImplementation(async path => {
      if (path === '/same') {
        return { entries: [{ name: 'src', path: '/same/src', isDirectory: true }] }
      }

      if (!childReady && seam === 'readDir') {
        return oldRead.promise
      }

      return listing('fresh.ts', path)
    })
    gitRoot.mockImplementation(async path =>
      !childReady && path === '/same/src' && seam === 'gitRoot' ? oldGit.promise : null
    )
    const { result } = renderHook(() => useProjectTree('/same'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    act(() => {
      void result.current.loadChildren('/same/src')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_001)
    })
    expect(result.current.data[0]).toMatchObject({ loading: false, error: 'read-timeout' })
    expect(result.current.data[0].children?.[0].placeholder).toBe('error')

    childReady = true
    await act(async () => {
      await result.current.loadChildren('/same/src')
    })
    expect(result.current.data[0].children?.map(node => node.name)).toEqual(['fresh.ts'])
    await act(async () => {
      oldRead.resolve(listing('obsolete.ts', '/same/src'))
      oldGit.resolve(null)
    })
    expect(result.current.data[0].children?.map(node => node.name)).toEqual(['fresh.ts'])
  })

  it('still self-heals an ordinary filesystem error after three seconds', async () => {
    readDir.mockResolvedValueOnce({ entries: [], error: 'EACCES' })
    const { result } = renderHook(() => useProjectTree('/same'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.rootError).toBe('EACCES')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_001)
    })
    expect(result.current.rootError).toBeNull()
    expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
  })

  it('propagates a fallback listing timeout and waits for a manual retry', async () => {
    const stalled = deferred<HermesReadDirResult>()
    let ready = false
    readDir.mockImplementation(async path =>
      path === '/same' ? { entries: [], error: 'ENOENT' } : ready ? listing('fallback.ts', path) : stalled.promise
    )
    sanitizeWorkspaceCwd.mockResolvedValue({ cwd: '/fallback', sanitized: true })
    const { result } = renderHook(() => useProjectTree('/same'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_001)
    })
    expect(result.current.rootLoading).toBe(false)
    expect(result.current.rootError).toBe('read-timeout')
    expect(result.current.effectiveCwd).toBe('/same')
    const readsAtTimeout = readDir.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_000)
    })
    expect(readDir.mock.calls.length).toBe(readsAtTimeout)
    ready = true
    await act(async () => {
      await result.current.refreshRoot()
    })
    expect(result.current.effectiveCwd).toBe('/fallback')
    expect(result.current.data.map(node => node.name)).toEqual(['fallback.ts'])
    await act(async () => {
      stalled.resolve(listing('obsolete.ts', '/fallback'))
    })
    expect(result.current.data.map(node => node.name)).toEqual(['fallback.ts'])
  })

  it('bounds a stalled cwd sanitizer without reading its late fallback', async () => {
    const stalled = deferred<{ cwd: string; sanitized: boolean }>()
    readDir.mockResolvedValueOnce({ entries: [], error: 'ENOENT' })
    sanitizeWorkspaceCwd.mockImplementationOnce(() => stalled.promise)
    const { result } = renderHook(() => useProjectTree('/same'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_001)
    })
    expect(result.current.rootLoading).toBe(false)
    expect(result.current.rootError).toBe('read-timeout')
    expect(result.current.effectiveCwd).toBe('/same')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_000)
    })
    expect(sanitizeWorkspaceCwd).toHaveBeenCalledTimes(1)
    await act(async () => {
      await result.current.refreshRoot()
    })
    expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
    await act(async () => {
      stalled.resolve({ cwd: '/obsolete-fallback', sanitized: true })
    })
    expect(readDir).not.toHaveBeenCalledWith('/obsolete-fallback')
    expect(result.current.effectiveCwd).toBe('/same')
  })

  it('does not read a default sanitizer result after switching to full', async () => {
    const stalled = deferred<{ cwd: string; sanitized: boolean }>()
    readDir.mockResolvedValueOnce({ entries: [], error: 'ENOENT' })
    sanitizeWorkspaceCwd.mockImplementationOnce(() => stalled.promise)
    $connection.set({ mode: 'local', connectionId: 'device', profile: 'default' } as never)
    const { result } = renderHook(() => useProjectTree('/same'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      $connection.set({ mode: 'local', connectionId: 'device', profile: 'full' } as never)
    })
    await act(async () => {
      stalled.resolve({ cwd: '/default-fallback', sanitized: true })
    })
    expect(readDir).not.toHaveBeenCalledWith('/default-fallback')
    expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
    expect(result.current.effectiveCwd).toBe('/same')
  })

  it('keeps one fallback recovery probe in flight and stops polling after its deadline', async () => {
    const stalled = deferred<HermesReadDirResult>()
    let originalReads = 0
    let ready = false
    readDir.mockImplementation(async path => {
      if (path === '/fallback') {
        return listing('fallback.ts', path)
      }

      originalReads += 1

      if (originalReads === 1) {
        return { entries: [], error: 'ENOENT' }
      }

      return ready ? listing() : stalled.promise
    })
    sanitizeWorkspaceCwd.mockResolvedValue({ cwd: '/fallback', sanitized: true })
    const { result } = renderHook(() => useProjectTree('/same'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.effectiveCwd).toBe('/fallback')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_001)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_000)
    })
    expect(originalReads).toBe(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(40_001)
    })
    expect(originalReads).toBe(2)
    expect(result.current.data.map(node => node.name)).toEqual(['fallback.ts'])
    ready = true
    await act(async () => {
      await result.current.refreshRoot()
    })
    expect(result.current.effectiveCwd).toBe('/same')
    expect(result.current.data.map(node => node.name)).toEqual(['current.ts'])
  })
})
