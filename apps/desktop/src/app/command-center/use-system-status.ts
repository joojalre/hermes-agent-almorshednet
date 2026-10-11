import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { $apiRequestScope } from '@/api/client'
import { getLogs, getStatus } from '@/hermes'
import type { StatusResponse } from '@/hermes'

export function useSystemStatus(active: boolean, file: string, level: string) {
  const scope = useStore($apiRequestScope)
  const scopeKey = JSON.stringify([scope.connectionId, scope.profile])
  const generation = useRef(0)
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const invalidate = useCallback(() => {
    ++generation.current
  }, [])

  const refresh = useCallback(
    async (withLogs = true) => {
      const request = ++generation.current
      setLoading(true)
      setError('')

      const results = await Promise.allSettled([
        getStatus(),
        withLogs ? getLogs({ file, level, lines: 200 }) : Promise.resolve(null)
      ])

      const current = $apiRequestScope.get()

      if (request !== generation.current || JSON.stringify([current.connectionId, current.profile]) !== scopeKey) {
        return
      }
      const [nextStatus, nextLogs] = results
      setStatus(nextStatus.status === 'fulfilled' ? nextStatus.value : null)

      if (nextLogs.status === 'fulfilled' && nextLogs.value) {
        setLogs(nextLogs.value.lines)
      }
      const failed = results.find(result => result.status === 'rejected')

      if (failed?.status === 'rejected') {
        setError(String(failed.reason instanceof Error ? failed.reason.message : failed.reason))
      }

      setLoading(false)
    },
    [file, level, scopeKey]
  )

  useEffect(() => {
    setStatus(null)
    setLogs([])
    setError('')
  }, [scopeKey])

  useEffect(() => {
    if (!active) {
      return
    }
    let disposed = false
    let timer: number

    const poll = async () => {
      if (document.visibilityState === 'visible' && document.hasFocus()) {
        await refresh(false)
      }

      if (!disposed) {
        timer = window.setTimeout(() => void poll(), 5000)
      }
    }

    void refresh().finally(() => {
      if (!disposed) {
        timer = window.setTimeout(() => void poll(), 5000)
      }
    })

    return () => {
      disposed = true
      invalidate()
      window.clearTimeout(timer)
    }
  }, [active, refresh, invalidate])

  return { status, logs, loading, error, setError, refresh, scopeKey }
}
