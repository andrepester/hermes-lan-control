// Native compatibility probe. Uses an existing built Hermes release, fresh
// per-run data directories, inert provider credentials, and loopback only.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { spawn, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { parseArgs } from 'node:util'

const { values: options, positionals } = parseArgs({
  args: process.argv.slice(2), allowPositionals: true,
  options: {
    'expected-revision': { type: 'string' }, candidate: { type: 'boolean', default: false },
    'no-prewarm': { type: 'boolean', default: false }
  }
})
const [sourceArg, pythonArg, runtimeArg, stateArg] = positionals
if (![sourceArg, pythonArg, runtimeArg, stateArg].every(value => value && path.isAbsolute(value))) {
  throw new Error('Usage: node check_native_compatibility.mjs ABS_RELEASE_SOURCE ABS_TEST_PYTHON ABS_NODE_RUNTIME_ROOT ABS_NEW_STATE_ROOT')
}
const source = fs.realpathSync(sourceArg), python = path.resolve(pythonArg)
const state = path.resolve(stateArg), runtime = fs.realpathSync(runtimeArg)
assert(!fs.existsSync(state), 'Use a new state directory; existing profiles must never be reused')
assert(!state.startsWith(source + path.sep), 'Keep test data outside the release source')
const desktop = path.join(source, 'apps/desktop')
assert(fs.existsSync(path.join(desktop, 'dist/electron-main.mjs')), 'Build the selected release first')
const require = createRequire(path.join(runtime, 'package.json'))
const { _electron } = require('playwright')
const yaml = require('yaml')
const desktopRequire = createRequire(path.join(desktop, 'package.json'))
const electronExecutable = desktopRequire('electron')
const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const expectedRevision = options['expected-revision'] || fs.readFileSync(path.join(project, 'tests/hermes-revision.txt'), 'utf8').trim()
assert(/^[a-f0-9]{40}$/.test(expectedRevision), 'An exact reviewed revision is required')
assert(!options.candidate || options['expected-revision'], 'Candidate probes require an explicit reviewed revision')
assert(!options['no-prewarm'] || options.candidate, 'Legacy startup is only enabled for explicit candidate probes')
const stamp = JSON.parse(fs.readFileSync(path.join(source, 'install-stamp.json'), 'utf8'))
assert.equal(stamp.commit, expectedRevision, 'Use build identity matching the reviewed Hermes revision')
assert.equal(stamp.dirty, false, 'The selected build must identify clean release source')
if (options.candidate || options['expected-revision']) {
  const desktopStamp = JSON.parse(fs.readFileSync(path.join(desktop, 'build/install-stamp.json'), 'utf8'))
  assert.equal(desktopStamp.commit, expectedRevision, 'Candidate Desktop must be built from the same release')
  assert.equal(desktopStamp.dirty, false, 'Candidate Desktop must identify clean source')
}
const home = path.join(state, 'hermes-home'), userData = path.join(state, 'desktop-data')
fs.mkdirSync(home, { recursive: true })
fs.mkdirSync(userData, { recursive: true })
const plugin = path.join(home, 'plugins/lan-control')
for (const name of ['plugin.yaml', '__init__.py', 'desktop/plugin.js', 'dashboard/manifest.json', 'dashboard/plugin_api.py']) {
  const target = path.join(plugin, name)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.copyFileSync(path.join(project, name), target)
}
if (options.candidate) {
  // Test runtime compatibility instead of merely observing that the current
  // supported-version policy blocks an older release. Source code is unchanged.
  const manifestFile = path.join(plugin, 'plugin.yaml')
  const candidate = yaml.parse(fs.readFileSync(manifestFile, 'utf8'))
  delete candidate.requires_hermes
  fs.writeFileSync(manifestFile, yaml.stringify(candidate))
}
let authorizedHealthRequests = 0
const provider = http.createServer((request, response) => {
  if (request.url.endsWith('/health') && request.headers.authorization === 'Bearer synthetic-health-check') authorizedHealthRequests++
  response.setHeader('content-type', 'application/json')
  response.end(JSON.stringify({ object: 'list', data: [{ id: 'compatibility-test', object: 'model' }] }))
})
await new Promise(resolve => provider.listen(0, '127.0.0.1', resolve))
const baseUrl = `http://127.0.0.1:${provider.address().port}/v1`
fs.writeFileSync(path.join(home, 'config.yaml'), yaml.stringify({
  model: { default: 'compatibility-test', provider: 'custom', base_url: baseUrl, context_length: 64000 },
  custom_providers: [{ name: 'Compatibility fixture', base_url: baseUrl, key_env: 'OPENAI_API_KEY' }],
  auxiliary: { title_generation: { enabled: false } },
  onboarding: { run: false },
  plugins: { enabled: ['lan-control'], entries: { 'lan-control': { settings: { display_name: 'Compatibility test', host: '127.0.0.1' } } } }
}))
fs.writeFileSync(path.join(home, '.env'), 'OPENAI_API_KEY=synthetic-not-issued\n', { mode: 0o600 })
const env = Object.fromEntries(['PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SHELL', 'DISPLAY', 'XDG_RUNTIME_DIR']
  .filter(key => process.env[key]).map(key => [key, process.env[key]]))
// HOME names the child process's actual disposable home. The caller's shell
// environment is untouched; no credentials or inherited Hermes overrides pass.
Object.assign(env, {
  HOME: state, HERMES_HOME: home, HERMES_DESKTOP_USER_DATA_DIR: userData,
  HERMES_DESKTOP_HERMES_ROOT: source, HERMES_DESKTOP_PYTHON: python,
  HERMES_DESKTOP_IGNORE_EXISTING: '1', HERMES_DESKTOP_ISOLATED_BACKEND: '1',
  HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION: '1', HERMES_DESKTOP_SKIP_QUIT_CONFIRM: '1',
  HERMES_DESKTOP_APP_NAME: `LANCompatibility-${Date.now()}`,
  HERMES_DISABLE_LAZY_INSTALLS: '1', PYTHONDONTWRITEBYTECODE: '1'
})
let app, page, prewarmedGateway = false
try {
  const guard = spawnSync(python, ['-B', '-c', `
import sitecustomize, sys
assert sitecustomize.LAN_CONTROL_NATIVE_UDP_GUARD is True
try:
    sys.audit('socket.sendto', None, ('127.0.0.1', 9))
except RuntimeError:
    pass
else:
    raise AssertionError('UDP negative control was not intercepted')
`], { cwd: source, env: { ...env, PYTHONPATH: source }, encoding: 'utf8' })
  assert.equal(guard.status, 0, 'Install tests/native_no_udp.py as sitecustomize.py in the dedicated test venv first')
  if (!options['no-prewarm']) {
    console.log('Starting the disposable gateway before the native renderer')
    const warm = spawn(python, ['-B', '-m', 'hermes_cli.main', 'gateway', 'ensure', '--json', '--timeout', '120'], {
      cwd: source, env: { ...env, PYTHONPATH: source }
    })
    prewarmedGateway = true
    const warmLog = fs.createWriteStream(path.join(state, 'gateway-start.log'))
    warm.stdout.pipe(warmLog, { end: false }); warm.stderr.pipe(warmLog, { end: false })
    const warmed = await new Promise((resolve, reject) => {
      warm.once('error', reject); warm.once('exit', resolve)
    })
    assert.equal(warmed, 0, 'Disposable gateway failed to start; inspect gateway-start.log')
  }
  console.log(`Launching isolated native probe: ${state}`)
  app = await _electron.launch({
    executablePath: electronExecutable,
    args: [desktop, '--disable-gpu'], cwd: desktop, env, timeout: 30000
  })
  const child = app.process()
  const log = fs.createWriteStream(path.join(state, 'electron.log'))
  child.stdout?.pipe(log, { end: false })
  child.stderr?.pipe(log, { end: false })
  const identity = await app.evaluate(({ app }) => ({
    userData: app.getPath('userData'), nativeHome: app.getPath('home'),
    environmentHome: process.env.HOME, hermesHome: process.env.HERMES_HOME
  }))
  assert.equal(fs.realpathSync(identity.userData), fs.realpathSync(userData))
  // macOS Electron's native account home ignores child HOME. Hermes' explicit
  // data-path overrides and the Python backend's environment home must agree.
  assert.equal(fs.realpathSync(identity.environmentHome), fs.realpathSync(state))
  assert.equal(fs.realpathSync(identity.hermesHome), fs.realpathSync(home))
  page = await app.firstWindow()
  await page.getByRole('button', { name: 'Open settings', exact: true }).waitFor({ timeout: 90000 })
  await page.locator('[data-slot="statusbar"]').getByText(/^(ready|bereit)$/).waitFor({ timeout: 90000 })
  const consent = page.getByRole('button', { name: /^(No thanks|Nein danke)$/ })
  if (await consent.count()) await consent.click()
  await page.getByRole('button', { name: /^(Capabilities|Fähigkeiten)$/ }).click()
  await page.getByRole('button', { name: 'Plugins', exact: true }).click({ timeout: 20000 })
  const diskRoot = await page.evaluate(() => window.hermesDesktop.desktopPluginsRoot())
  assert.equal(fs.realpathSync(diskRoot), fs.realpathSync(path.join(home, 'desktop-plugins')))
  const row = page.locator('[data-testid^="plugin-row-"]').filter({ hasText: /LAN[ -]Control/i })
  // Earlier releases render installed plugins as inline rows; later releases
  // use a searchable catalog with a modal package detail.
  await row.or(page.getByRole('textbox').first()).first().waitFor({ timeout: 20000 })
  const missingExport = row.getByText(/does not provide an export named/)
  if (await missingExport.count()) throw new Error(`Incompatible SDK: ${await missingExport.innerText()}`)
  const inlineRow = await row.count() > 0 && await row.isVisible()
  if (!inlineRow) {
    await page.getByRole('textbox').first().fill('LAN Control')
    await page.getByRole('button', { name: /^LAN Control$/i }).click({ timeout: 20000 })
  }
  await row.waitFor({ timeout: 20000 })
  const desktopSwitch = row.getByRole('switch', { name: /^Desktop:/ })
  if (await desktopSwitch.getAttribute('aria-checked') !== 'true') await desktopSwitch.click()
  if (!inlineRow) await page.getByRole('button', { name: /^(Close|Schließen)$/ }).last().click()
  await page.locator('[data-slot="statusbar"]').getByText('Compatibility test', { exact: true }).waitFor({ timeout: 20000 })
  await page.getByRole('img', { name: /^Compatibility test: Checks passed/ }).waitFor({ timeout: 20000 })
  assert(await page.getByRole('button', { name: 'Wake-on-LAN', exact: true }).isDisabled())
  await page.getByRole('button', { name: 'Compatibility test: check again', exact: true }).click()
  if (!inlineRow) await page.getByRole('button', { name: /^LAN Control$/i }).click()
  await row.getByRole('button', { name: /^(Settings|Einstellungen): LAN Control$/i }).click()
  await page.getByRole('textbox', { name: 'Display name', exact: true }).fill('Native compatibility verified')
  await page.getByRole('textbox', { name: 'Health URL (optional)', exact: true }).fill(`${baseUrl}/health`)
  await page.getByLabel('Bearer token (optional)', { exact: true }).fill('synthetic-health-check')
  await page.getByRole('button', { name: 'Save settings', exact: true }).click()
  await page.getByRole('img', { name: /^Native compatibility verified: Checks passed/, includeHidden: true }).waitFor({ timeout: 20000 })
  assert(authorizedHealthRequests > 0, 'Native HTTP health check must receive the synthetic bearer token')
  assert(await page.getByRole('button', { name: 'Wake-on-LAN', exact: true, includeHidden: true }).isDisabled())
  const stored = yaml.parse(fs.readFileSync(path.join(home, 'config.yaml'), 'utf8'))
  assert.equal(stored.plugins.entries['lan-control'].settings.display_name, 'Native compatibility verified')
  assert(fs.readFileSync(path.join(home, '.env'), 'utf8').includes('LAN_CONTROL_BEARER_TOKEN=synthetic-health-check'))
  console.log('PASS: native package discovery, SDK activation, loopback ping/HTTP, manual recheck, settings and secret persistence; wake disabled')
  const body = await page.locator('body').innerText()
  fs.writeFileSync(path.join(state, 'native-ui.txt'), body)
  await page.screenshot({ path: path.join(state, 'native-ui.png') })
  const digest = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex')
  const result = {
    passed: true, hermes_version: stamp.baseVersion, hermes_revision: stamp.commit,
    candidate_probe: options.candidate,
    metadata_override: options.candidate ? 'requires_hermes removed only in disposable plugin copy' : null,
    platform: process.platform, node: process.version,
    desktop_bundle_sha256: digest(path.join(desktop, 'dist/electron-main.mjs')),
    plugin_files_sha256: Object.fromEntries(['plugin.yaml', '__init__.py', 'desktop/plugin.js',
      'dashboard/manifest.json', 'dashboard/plugin_api.py'].map(name => [name, digest(path.join(plugin, name))])),
    repository_plugin_files_sha256: Object.fromEntries(['plugin.yaml', '__init__.py', 'desktop/plugin.js',
      'dashboard/manifest.json', 'dashboard/plugin_api.py'].map(name => [name, digest(path.join(project, name))])),
    checks: ['package discovery', 'SDK activation', 'loopback ping', 'authenticated loopback HTTP',
      'manual recheck', 'native settings page', 'config persistence', 'secret persistence', 'wake disabled', 'Python UDP send guard'],
    authorized_health_requests: authorizedHealthRequests
  }
  fs.writeFileSync(path.join(state, 'result.json'), JSON.stringify(result, null, 2) + '\n')
  console.log(JSON.stringify(result, null, 2))
} catch (error) {
  if (page && !page.isClosed()) {
    fs.writeFileSync(path.join(state, 'failed-ui.txt'), await page.locator('body').innerText())
    await page.screenshot({ path: path.join(state, 'failed-ui.png') })
    console.log('Native failure UI:', (await page.locator('body').innerText()).slice(0,6000))
    console.log('Plugin status:', await page.locator('.lan-control__dot').getAttribute('aria-label', { timeout: 1000 }).catch(() => null))
  }
  throw error
} finally {
  if (app) await app.close()
  if (prewarmedGateway) {
    const stop = spawnSync(python, ['-B', '-m', 'hermes_cli.main', 'gateway', 'stop'], {
      cwd: source, env: { ...env, PYTHONPATH: source }, timeout: 20000, encoding: 'utf8'
    })
    fs.writeFileSync(path.join(state, 'gateway-stop.log'), (stop.stdout || '') + (stop.stderr || ''))
    console.log(`Isolated gateway cleanup exit: ${stop.status}`)
  }
  await new Promise(resolve => provider.close(resolve))
}
