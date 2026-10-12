import type { SourceBackend } from './source-backend'

interface SourceRootDependencies {
  canonicalRoot: string
  isSourceRoot: (root: string) => boolean
  isManagedRoot: (root: string) => boolean
  developerPython: () => boolean
  sourcePython: (root: string, args: string[]) => Promise<SourceBackend | null>
  installation: (root: string, args: string[]) => Promise<SourceBackend | null>
  publishedInstall: () => { root: string; launcher: string } | null
  installedGate: {
    resolve: <T>(root: string, probe: () => Promise<T | null>) => Promise<T | null>
  }
  log: (message: string) => void
}

/** The backend and checkout updater must select the same installation. */
export function createSourceRootSelection(deps: SourceRootDependencies) {
  let selectedRoot: string | null = null
  let unresolvedCommand = false

  function selected(backend: SourceBackend | null): SourceBackend | null {
    if (backend) {
      selectedRoot = backend.root
      unresolvedCommand = false
    }

    return backend
  }

  return {
    record(root: string | null): void {
      selectedRoot = root
      unresolvedCommand = !root
    },

    async explicit(root: string | undefined, args: string[]): Promise<SourceBackend | null> {
      if (!root) {return null}

      if (!deps.isSourceRoot(root)) {throw new Error(`Invalid explicit Hermes source root: ${root}`)}

      // Developer interpreters are authoritative. Otherwise PM owns the runtime;
      // a checkout venv is only the pre-PM source-install compatibility rung.
      const backend = deps.developerPython()
        ? await deps.sourcePython(root, args)
        : (await deps.installation(root, args)) ??
          (deps.isManagedRoot(root) ? null : await deps.sourcePython(root, args))

      if (!backend) {throw new Error(`No usable runtime for explicit Hermes source root: ${root}`)}

      return selected(backend)
    },

    async installed(args: string[]): Promise<SourceBackend | null> {
      const published = deps.publishedInstall()

      if (published) {
        const backend = await deps.installedGate.resolve(published.root, () => deps.installation(published.root, args))

        if (backend) {
          deps.log(`[boot] Using Hermes install at ${published.root} (published launcher ${published.launcher})`)

          return selected(backend)
        }
      }

      return selected(await deps.installedGate.resolve(deps.canonicalRoot, () => deps.installation(deps.canonicalRoot, args)))
    },

    updateRoot(candidates: string[]): string {
      if (unresolvedCommand) {throw new Error('The explicit Hermes command did not report an installation root; checkout updates are unavailable.')}

      return selectedRoot || candidates.find(root => root !== deps.canonicalRoot) ||
        deps.publishedInstall()?.root || deps.canonicalRoot
    }
  }
}
