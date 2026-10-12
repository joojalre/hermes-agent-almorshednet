import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, test, vi } from 'vitest'

import { createInstalledRuntimeGate } from './backend-resolution'
import { createSourcePythonBackend, resolveSourceInstallationBackend } from './source-backend'
import { resolveSourcePython } from './source-python'
import { createSourceRootSelection } from './source-root-selection'
import { launcherInstallDirectory, userLauncherInstallRoot } from './updater-process'

let temp: string
let home: string
let canonical: string
let published: string

function source(root: string, usable = true): string {
  fs.mkdirSync(path.join(root, 'hermes_cli'), { recursive: true })
  fs.mkdirSync(path.join(root, 'pm'), { recursive: true })
  fs.writeFileSync(path.join(root, 'hermes_cli', 'main.py'), '')
  const bin = path.join(root, '.hermes', 'bin')
  fs.mkdirSync(bin, { recursive: true })
  const command = path.join(bin, process.platform === 'win32' ? 'hermes.cmd' : 'hermes')
  fs.writeFileSync(command, process.platform === 'win32'
    ? `@echo off\r\necho Install directory: ${root}\r\nexit /b ${usable ? 0 : 2}\r\n`
    : `#!/bin/sh\nprintf '%s\\n' 'Install directory: ${root}'\nexit ${usable ? 0 : 2}\n`, { mode: 0o755 })

  return command
}

function publish(root: string): void {
  const bin = path.join(home, 'bin')
  fs.mkdirSync(bin, { recursive: true })
  fs.copyFileSync(path.join(root, '.hermes', 'bin', process.platform === 'win32' ? 'hermes.cmd' : 'hermes'),
    path.join(bin, process.platform === 'win32' ? 'hermes.cmd' : 'hermes'))
}

beforeEach(() => {
  temp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-source-roots-')))
  home = path.join(temp, 'home')
  canonical = path.join(home, 'hermes-agent')
  published = path.join(temp, 'published source')
  source(canonical)
  source(published)
  publish(published)
  vi.stubEnv('HOME', home)
  vi.stubEnv('USERPROFILE', home)
  vi.stubEnv('HERMES_HOME', home)
  vi.stubEnv('LOCALAPPDATA', path.join(temp, 'Local'))
  vi.stubEnv('HERMES_INSTALL_ROOT', '')
})

afterEach(() => {
  vi.unstubAllEnvs()
  fs.rmSync(temp, { recursive: true, force: true })
})

function selection(developerPython?: string) {
  const probes: string[] = []
  const env: NodeJS.ProcessEnv = { ...process.env, HERMES_HOME: home }
  const gate = createInstalledRuntimeGate(env, () => {})

  const roots = createSourceRootSelection({
    canonicalRoot: canonical,
    isSourceRoot: root => fs.existsSync(path.join(root, 'hermes_cli', 'main.py')),
    isManagedRoot: root => fs.existsSync(path.join(root, 'pm')),
    developerPython: () => Boolean(developerPython),
    sourcePython: async (root, args) => createSourcePythonBackend(root, resolveSourcePython(root, { override: developerPython }), args, { env }),
    installation: async (root, args) => {
      probes.push(root)

      return resolveSourceInstallationBackend(root, args, { hermesHome: home, env })
    },
    publishedInstall: () => userLauncherInstallRoot(process.platform === 'win32', home),
    installedGate: gate,
    log: () => {}
  })

  return { roots, probes, env, gate }
}

test('a ROOT-only PM pin uses its managed launcher without a checkout venv', async () => {
  const { roots, probes } = selection()
  const backend = await roots.explicit(published, ['--profile', 'worker', 'serve'])
  assert.equal(backend?.root, published)
  assert.ok(backend?.command.includes(path.join(published, '.hermes', 'bin')))
  assert.deepEqual(backend?.args, ['--profile', 'worker', 'serve'])
  assert.deepEqual(probes, [published])
  assert.equal(roots.updateRoot([canonical]), published)
})

test('an explicit developer interpreter wins over the managed installation', async () => {
  const { roots, probes } = selection(process.execPath)
  const backend = await roots.explicit(published, ['serve'])
  assert.equal(backend?.kind, 'python')
  assert.equal(backend?.command, process.execPath)
  assert.equal(backend?.env.PYTHONPATH, published)
  assert.deepEqual(probes, [])
})

test.each(['missing', 'broken', 'missing-launcher'])('an explicit %s root refuses a different healthy installation', async state => {
  const root = path.join(temp, state)

  if (state !== 'missing') {
    const command = source(root, state !== 'broken')

    if (state === 'missing-launcher') {fs.unlinkSync(command)}
    const stalePython = path.join(root, 'venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
    fs.mkdirSync(path.dirname(stalePython), { recursive: true })
    fs.copyFileSync(process.execPath, stalePython)
  }

  const { roots, probes } = selection()
  await assert.rejects(roots.explicit(root, ['serve']), /explicit Hermes source root/i)
  assert.ok(!probes.includes(canonical))
  assert.ok(!probes.includes(published))
})

test('a selected development source keeps updater authority over a published installation', () => {
  const { roots } = selection()
  roots.record(canonical)
  assert.equal(roots.updateRoot([published]), canonical)
})

test('an explicit CLI installation keeps updater authority over the published installation', () => {
  const { roots } = selection()
  const command = path.join(canonical, '.hermes', 'bin', process.platform === 'win32' ? 'hermes.cmd' : 'hermes')
  const root = launcherInstallDirectory(command)
  assert.equal(root, canonical)
  roots.record(root)
  assert.equal(roots.updateRoot([published]), canonical)
})

test('an explicit command without installation identity cannot update a different checkout', () => {
  const { roots } = selection()
  roots.record(null)
  assert.throws(() => roots.updateRoot([canonical]), /explicit Hermes command/)
})

test('a published installation wins over a retained canonical checkout for backend and updater', async () => {
  const { roots, probes } = selection()
  const backend = await roots.installed(['serve'])
  assert.equal(backend?.root, published)
  assert.deepEqual(probes, [published])
  assert.equal(roots.updateRoot([canonical]), published)
})

test('an unusable published launcher falls back to the healthy canonical installation', async () => {
  source(published, false)
  const { roots } = selection()
  assert.equal((await roots.installed(['serve']))?.root, canonical)
  assert.equal(roots.updateRoot([canonical]), canonical)
})

test('ignore-existing still skips both installations until this launch installs a runtime', async () => {
  const { roots, probes, env, gate } = selection()
  env.HERMES_DESKTOP_IGNORE_EXISTING = '1'
  assert.equal(await roots.installed(['serve']), null)
  assert.deepEqual(probes, [])
  assert.equal((await gate.afterInstall(() => roots.installed(['serve'])))?.root, published)
})
