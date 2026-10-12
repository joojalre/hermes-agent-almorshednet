import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import type { ActionStatusResponse, StatusResponse } from '@/hermes'
import { useI18n } from '@/i18n'
import type { Translations } from '@/i18n/types'
import { cn } from '@/lib/utils'

interface SystemStatusSummaryProps {
  status: StatusResponse | null
  systemBusy: boolean
  systemLoading: boolean
  actionStarting: 'restart' | 'update' | null
  restarting: boolean
  desktopUpdate: { applying: boolean }
  backendUpdate: { applying: boolean }
  systemAction: ActionStatusResponse | null
  runSystemAction: (kind: 'restart' | 'update') => Promise<void>
  refreshSystem: (withLogs?: boolean) => Promise<void>
}

function gatewayLabel(props: SystemStatusSummaryProps, t: Translations): string {
  const { status, systemBusy, actionStarting, restarting, desktopUpdate, backendUpdate, systemAction } = props
  const cc = t.commandCenter

  return actionStarting === 'update' ||
    desktopUpdate.applying ||
    backendUpdate.applying ||
    (systemAction?.running && systemAction.name === 'hermes-update')
    ? t.updates.installing
    : restarting || systemBusy
      ? t.shell.statusbar.gatewayRestarting
      : status?.gateway_heartbeat_stale_s || status?.gateway_state === 'degraded'
        ? cc.gatewayDegraded
        : status?.gateway_state === 'draining' || status?.gateway_state === 'stopping'
          ? cc.gatewayDraining
          : status?.gateway_state === 'starting'
            ? t.messaging.states.connecting
            : status?.gateway_running
              ? cc.gatewayRunning
              : cc.gatewayStopped
}

/** Presentation only: polling and action leases stay mounted with Command Center. */
export function SystemStatusSummary(props: SystemStatusSummaryProps) {
  const { status, systemBusy, systemLoading, systemAction, runSystemAction, refreshSystem } = props
  const { t } = useI18n()
  const cc = t.commandCenter

  return (
    <div>
      {status || systemBusy ? (
        <div className="grid gap-2">
          <div className="flex items-start justify-between gap-3 max-[47.5rem]:flex-col max-[47.5rem]:gap-2">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    'size-2 shrink-0 rounded-full',
                    status?.gateway_running &&
                      !status.gateway_heartbeat_stale_s &&
                      status.gateway_state === 'running' &&
                      !systemBusy
                      ? 'bg-emerald-500'
                      : 'bg-amber-500'
                  )}
                />
                <span className="text-[length:var(--conversation-text-font-size)] font-medium text-foreground">
                  {gatewayLabel(props, t)}
                </span>
              </div>
              <div className="mt-1 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                {status ? cc.hermesActiveSessions(status.version, status.active_sessions) : t.updates.checkingShort}
                {' · '}
                <a href="https://github.com/NousResearch/hermes-agent" rel="noreferrer" target="_blank">
                  NousResearch/hermes-agent
                </a>
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 whitespace-nowrap max-[47.5rem]:whitespace-normal">
              <Button disabled={systemBusy} onClick={() => void runSystemAction('restart')} size="xs" variant="text">
                {cc.restartGateway}
              </Button>
              <Button
                disabled={systemBusy}
                onClick={() => void runSystemAction('update')}
                size="xs"
                variant="textStrong"
              >
                {cc.updateHermes}
              </Button>
            </div>
          </div>
          {systemAction && (
            <div className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
              {systemAction.name} ·{' '}
              {systemAction.running
                ? cc.actionRunning
                : systemAction.exit_code == null
                  ? cc.actionStartedWaiting
                  : systemAction.exit_code === 0
                    ? cc.actionDone
                    : cc.actionFailed}
            </div>
          )}
        </div>
      ) : systemLoading ? (
        <PageLoader className="min-h-32" label={cc.loadingStatus} />
      ) : (
        <Button onClick={() => void refreshSystem()} size="xs" variant="text">
          {cc.retry}
        </Button>
      )}
    </div>
  )
}
