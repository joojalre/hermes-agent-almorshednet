# Upstream `main` Merge Provenance (2026-10-02)

## Objective

Bring `joojalre/hermes-agent-almorshednet` up to the current upstream `main` with a merge
commit, keeping fork-specific behavior that the new upstream tree does not already supersede.

Unlike the v0.21.0 reconciliation (`2026-09-01-hermes-v0.21.0-candidate-provenance.md`), this
is a full merge of the post-release upstream wave, requested explicitly by the fork owner. It is
delivered as a draft pull request and must not be merged without the owner's go-ahead.

## Immutable inputs

| Input | Commit |
|---|---|
| Upstream `NousResearch/hermes-agent` `main` | `bfc7152687277dd877734e8e33ad0dd6bbbfa07d` |
| Fork `main` (first parent) | `7389dc7172fc7a3fada7294a8d3659b305a2494c` |
| Merge base | `749220ef0007f8d87bd1531f1c24b0fe93816385` |

Upstream is 5,275 commits ahead of the merge base; the fork is 206 commits ahead. The merge
produced 160 conflicted paths; every one was resolved by hand (no whole-side checkout of the
tree), then re-verified against both parents.

## What changes for an installed fork

- **Runtime and toolchain.** Upstream now runs on a package-manager (PM) managed runtime:
  Python 3.14, uv 0.12.3, npm 12.0.2 and Node 26.7.0, provisioned by the `setup-pm` composite
  action and `./activate`. `uv.lock` only supports Python 3.14.
- **Update subsystem.** `hermes update` was rewritten upstream (`update_completion`,
  `_old_updater`, takeover, serve obligations). The post-swap handoff the fork extended no longer
  exists.
- **Versioning.** The source version is dynamic (`0.0.0` in the tree).
- **Lazy dependencies** are retired (`tools/lazy_deps.py` is a shim).

An installed fork that runs `hermes update` against this branch crosses all of these at once.

## Resolution rules

1. Upstream implementations win where they already deliver or supersede a fork fix.
2. Fork behavior that upstream lacks is kept and re-attached to the new upstream structure.
3. A fork test whose subject no longer exists upstream is removed only when an upstream
   mechanism with its own coverage replaces it (listed below).
4. Every security pin is kept at the stricter of the two sides.

## Fork behavior preserved (re-attached to the upstream structure)

- **CI on a fork:** public runners only for the policy-checked workflows
  (`tests-os.yml`, `nix.yml`, `tests.yml`, `js-tests.yml`, `rust-tests.yml`), `nix flake check
  --max-jobs 2` with a 90-minute budget, fork-opt-in validation guards, the `prepare` job with
  four test slices, OSV status emission, and the narrow review-gate rerun. Workflows outside the
  policy test use `github.repository == 'NousResearch/hermes-agent'` to select the private
  runner upstream and the public runner here.
- **Action pins:** `actions/checkout` is pinned to the real `v7.0.1` tag commit
  (`3d3c42e5aac5ba805825da76410c181273ba90b1`); upstream's pin labelled `v7.0.1` was the
  `main` branch commit.
- **Security pins:** `h2==4.4.1` override (GHSA-6hr6-w5qg-qmwg), Electron `41.10.6`,
  `js-yaml 4.3.2`, `browserslist 4.29.0`, `ip-address 10.7.2`.
- **`hermes update --no-zip-fallback`:** the flag survived the textual merge but its behavior
  did not, so the flag would have been silently ignored. The refusal is re-attached to the
  upstream Git-failure handler and to the direct ZIP entry point.
- **Windows gateway logon persistence:** no VBScript. A hidden PowerShell `.lnk` Startup entry,
  and legacy `.vbs`/`.cmd` launchers are archived to `legacy-launchers` rather than deleted.
  Upstream's reconcile API (`reconcile_autostart_launchers`, `redundant_autostart_entries`) is
  implemented on top of that model, and its tests were rewritten for it.
- **Plugin install containment:** symlinks escaping the staged plugin tree are refused before
  any readability probe or permission repair (ported into `plugins_cmd_install`).
- **MCP:** multi-root npx cache resolution and the stdio preflight that sees the original
  invocation before the cached-binary swap; deferred per-session MCP refresh while a turn runs,
  now also forwarding `disabled_override` (upstream #44499).
- **Profile scope binding** (`tui_gateway/model_switch.py`): the fork's explicit
  launch-home detection, resolved through upstream's call-time `_launch_home()`.
- **Desktop:** profile-activation foreground priority, privacy-safe logging, the
  local-default startup-profile rule, the prewarm reservation that leaves one foreground slot
  free, "Open memory file" in Maintenance, and "Start Hermes with Windows" (types and
  translations restored for de, es, fr, ru, ar, ja, zh and zh-Hant where the fork had them).
- **ACP:** drive-relative path rejection on top of upstream's `file:///` conversion.
- **Vault settings after reconnect:** the item list query is `staleTime: 0`, so a cached
  list cannot outlive an unlock the backend revoked on disconnect.
- **Command Center log tail:** only the latest `getLogs` request may publish, so a slow
  response for the previously selected file or level cannot replace the newer selection.
  Upstream's own `refreshUsage` already used this guard; `refreshSystem` did not.
- **Windows updater handoff:** `cmd /d /v:off` with a Base64 PowerShell dispatcher and an
  allow-listed parameter set, so paths and branch names never reach cmd syntax.
- **Bot DM delivery:** pending (`queued`/`claimed`) live deliveries exit 0 with a "do not resend"
  receipt, matching upstream's bounded wait, so a sender never treats them as failures and
  duplicates the message.
- **Background review privacy:** no configuration values in logs.

## Fork behavior dropped or pending re-port

| Item | Status | Reason |
|---|---|---|
| Speculative / activation-handoff design in `apps/desktop/src/store/gateway.ts` | Pending re-port | Upstream rewrote the store. The fork's prewarm guard is kept in `profile.ts`, but prewarm now dials without the `speculative` flag and openGatewayAgent no longer takes an abort signal. The Electron side still accepts `speculative`. |
| Fork tests of that design in `gateway.test.ts` and `gateway-shared-remote.test.ts` | Replaced with upstream's | They test the dropped store design. |
| `update_cmd._execute_post_swap` / `_hand_off_post_swap` hardening and its tests (`test_update_stale_module_purge.py`, two tests in `test_update_serve_generation_recovery.py`) | Removed | The post-swap handoff no longer exists upstream; serve recovery is owned by `update_serve_obligations.py`. |
| `test_metadata_write_failure_restores_replaced_git_tree` | Removed | Plugin publication is now a journaled PM transaction, covered by `tests/pm/test_worker_publication.py::test_worker_death_recovers_at_each_durable_publication_boundary`. |
| `test_completed_process_results.py` 503-retry variant | Replaced with upstream's | Upstream counts new history occurrences, which covers retries and replays. |
| `tests/scripts/test_plugin_validate_action.py` | Moved | Its default-checkout invariant now lives in `tests/ci/test_plugin_validate_action.py` (same basename collided). |
| Fork split of `vault-settings.tsx` into `vault-settings-{add,add-dialog,data,form,sources}` | Removed | Upstream's single `vault-settings.tsx` is the live screen; the split modules only imported each other. The reconnect fix above was re-applied to upstream's file. |
| Fork Command Center log-filter UI tests (WARNING default, 100 lines, labelled groups, search placeholder) | Replaced | Upstream ships its own log file/level tabs and search; the late-response race test was kept and rewritten for upstream's UI. |
| Foreground-priority wake redial of the active secondary (`gateway-connection-lifecycle.test.ts` assertion) | Pending re-port | Part of the fork store design above. |
| `httpx2` | Kept at upstream `2.7.0` | No fork pin was newer. |
| `cachix/cachix-action` pin labelled `v17` | Unchanged | Pre-existing on every side; not the `v17` tag commit. Pending review. |

## Test-suite adaptations

- The legacy `linux_only` / `macos_only` / `windows_only` markers, which upstream now rejects at
  collection, were rewritten to `platforms(...)`. Stacked markers in `test_find_shell.py` and
  `test_process_registry_windows_live.py` were reduced to one.
- Fork CI-policy tests had silently skipped because PyYAML is no longer installed; they now
  use `hermes_yaml` and run. Their assertions were adapted to upstream's
  `scripts/ci/required_results.py` gate, the added `workflow_call` trigger, and the removed
  compat-pointer checker.

## Verification (this environment: Linux x86_64, Python 3.14.7, Node 22)

- `uv lock --check`: pass.
- `ruff check .` (blocking lint): pass.
- Full test collection: 59,627 tests, 0 collection errors.
- Desktop `typecheck` (renderer, Electron, e2e, builder config): pass. `ui-tui` typecheck: pass.
- Python and vitest suite results are reported on the pull request; failures that reproduce on
  clean upstream `main` in the same environment are listed there as pre-existing.

Not run here: native Windows and macOS lanes, desktop packaging, live provider tests, and the
Docker and Nix builds. CI on the pull request is the authority for those.
