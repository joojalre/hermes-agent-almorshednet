import { describe, expect, it, vi } from 'vitest'

import { BACKEND_BOOT_WAIT_TIMEOUT_MS, BACKEND_SWITCH_WAIT_TIMEOUT_MS, withTimeout } from './with-timeout'

describe('withTimeout', () => {
  it('keeps cold primary boot separate from connection-switch waits', () => {
    expect(BACKEND_BOOT_WAIT_TIMEOUT_MS).toBeGreaterThan(BACKEND_SWITCH_WAIT_TIMEOUT_MS)
    expect(BACKEND_BOOT_WAIT_TIMEOUT_MS).toBeGreaterThanOrEqual(90_000 + 45_000)
    expect(BACKEND_SWITCH_WAIT_TIMEOUT_MS).toBe(45_000)
  })

  it('rejects with an onTimeout exception instead of letting it escape the timer callback', async () => {
    vi.useFakeTimers()

    try {
      const callbackFailure = new Error('abort callback failed')

      const result = withTimeout(new Promise<never>(() => undefined), 10, 'work timed out', () => {
        throw callbackFailure
      })

      const rejection = expect(result).rejects.toBe(callbackFailure)

      await vi.advanceTimersByTimeAsync(10)
      await rejection
    } finally {
      vi.useRealTimers()
    }
  })
})
