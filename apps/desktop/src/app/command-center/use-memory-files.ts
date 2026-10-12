import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { $apiRequestScope } from '@/api/client'
import { getMemoryStatus, getStatus, type MemoryStatusResponse, resetMemory } from '@/hermes'
import { useI18n } from '@/i18n'
import { localPreviewTarget } from '@/lib/local-preview'
import { $confirmRequest, confirm, type PendingConfirm, settleConfirm } from '@/store/confirm'
import { notify, notifyError } from '@/store/notifications'
import { openPreview } from '@/store/preview'

function activeMemoryScopeKey() {
  const scope = $apiRequestScope.get()

  return JSON.stringify([scope.connectionId, scope.profile])
}

export function useMemoryFiles(onOpenFile?: () => void) {
  const { t } = useI18n()
  const mm = t.commandCenter.maintenance
  const scope = useStore($apiRequestScope)
  const scopeKey = JSON.stringify([scope.connectionId, scope.profile])
  const generation = useRef(0)
  const pendingConfirm = useRef<PendingConfirm | null>(null)
  const [snapshot, setSnapshot] = useState<{ scopeKey: string; data: MemoryStatusResponse } | null>(null)
  const [failure, setFailure] = useState<{ scopeKey: string; message: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [retry, setRetry] = useState(0)
  const memory = snapshot?.scopeKey === scopeKey ? snapshot.data : null
  const isCurrent = useCallback(
    (request: number) => request === generation.current && activeMemoryScopeKey() === scopeKey,
    [scopeKey]
  )

  // Request and confirmation ownership end when this panel leaves its profile.
  // eslint-disable-next-line no-restricted-syntax -- generation and the owned prompt are imperative request handles
  useEffect(() => {
    const request = ++generation.current
    setSnapshot(null)
    setFailure(null)
    setBusy(false)
    getMemoryStatus()
      .then(data => isCurrent(request) && setSnapshot({ scopeKey, data }))
      .catch(
        error =>
          isCurrent(request) &&
          setFailure({
            scopeKey,
            message: error instanceof Error ? error.message : String(error)
          })
      )

    return () => {
      ++generation.current

      if (pendingConfirm.current) {
        settleConfirm(false, pendingConfirm.current)
        pendingConfirm.current = null
      }
    }
  }, [isCurrent, retry, scopeKey])

  const doResetMemory = useCallback(
    async (target: 'all' | 'memory' | 'user', label: string) => {
      const request = generation.current

      if (!memory || busy || !isCurrent(request)) {
        return
      }

      setBusy(true)

      try {
        const previous = $confirmRequest.get()
        const answer = confirm({ destructive: true, title: mm.resetConfirm(label) })
        const pending = $confirmRequest.get()
        const owned = pending !== previous ? pending : null
        pendingConfirm.current = owned
        const accepted = await answer

        if (pendingConfirm.current === owned) {
          pendingConfirm.current = null
        }

        if (!accepted || !isCurrent(request)) {
          return
        }

        const result = await resetMemory(target)

        if (!isCurrent(request)) {
          return
        }

        notify({ kind: 'success', title: mm.resetDone(result.deleted.join(', ') || label), message: '' })
        const data = await getMemoryStatus()

        if (isCurrent(request)) {
          setSnapshot({ scopeKey, data })
        }
      } catch (error) {
        if (isCurrent(request)) {
          notifyError(error, mm.resetFailed)
        }
      } finally {
        if (isCurrent(request)) {
          setBusy(false)
        }
      }
    },
    [busy, isCurrent, memory, mm, scopeKey]
  )

  const openMemoryFile = useCallback(
    async (filename: 'MEMORY.md' | 'USER.md') => {
      const request = generation.current

      if (!memory || busy || !isCurrent(request)) {
        return
      }

      setBusy(true)

      try {
        const status = await getStatus()

        if (!isCurrent(request)) {
          return
        }

        if (!status.hermes_home) {
          throw new Error(t.errors.genericFailure)
        }

        const home = status.hermes_home.replace(/[\\/]+$/, '')
        const separator = home.includes('\\') ? '\\' : '/'
        const target = localPreviewTarget([home, 'memories', filename].join(separator))

        if (target) {
          openPreview(target)
          onOpenFile?.()
        }
      } catch (error) {
        if (isCurrent(request)) {
          notifyError(error, t.errors.genericFailure)
        }
      } finally {
        if (isCurrent(request)) {
          setBusy(false)
        }
      }
    },
    [busy, isCurrent, memory, onOpenFile, t.errors.genericFailure]
  )

  return {
    memory,
    memoryBusy: busy,
    memoryError: failure?.scopeKey === scopeKey ? failure.message : '',
    doResetMemory,
    openMemoryFile,
    retryMemory: () => setRetry(value => value + 1)
  }
}
