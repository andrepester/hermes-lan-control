// Exercise Hermes' real filesystem reconciliation without starting Electron.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const [source, home, phase] = process.argv.slice(2)
assert(source && home && ['installed', 'updated', 'removed'].includes(phase))
assert.equal(path.resolve(home), process.env.HERMES_HOME)
assert(process.env.HERMES_DESKTOP_USER_DATA_DIR.startsWith(path.dirname(home) + path.sep))

const { reconcileUnifiedDesktopHalves, PACKAGE_MARKER } = await import(pathToFileURL(
  path.join(source, 'apps/desktop/electron/desktop-plugins-root.ts')
).href)
const appRoot = path.join(home, 'desktop-plugins')
const target = path.join(appRoot, 'lan-control')
const standalone = path.join(appRoot, 'unrelated-standalone')
await fs.mkdir(standalone, { recursive: true })
await fs.writeFile(path.join(standalone, 'plugin.js'), '// unrelated standalone\n')
await reconcileUnifiedDesktopHalves(home, appRoot)

if (phase === 'removed') {
  await assert.rejects(fs.stat(target), { code: 'ENOENT' })
} else {
  const original = path.join(home, 'plugins/lan-control/desktop')
  const marker = JSON.parse(await fs.readFile(path.join(target, PACKAGE_MARKER), 'utf8'))
  assert.equal(marker.package, 'lan-control')
  assert.equal(marker.source, original)
  assert.match(marker.repo, /^file:\/\//)
  assert.deepEqual(await fs.readFile(path.join(target, 'plugin.js')), await fs.readFile(path.join(original, 'plugin.js')))
  assert.equal((await reconcileUnifiedDesktopHalves(home, appRoot)).length, 0, 'reconcile must be idempotent')

  // A conflicting standalone installation must never be adopted or overwritten.
  const collisionRoot = path.join(home, 'standalone-collision')
  const collision = path.join(collisionRoot, 'lan-control')
  await fs.mkdir(collision, { recursive: true })
  const sentinel = '// user-owned standalone\n'
  await fs.writeFile(path.join(collision, 'plugin.js'), sentinel)
  await reconcileUnifiedDesktopHalves(home, collisionRoot)
  assert.equal(await fs.readFile(path.join(collision, 'plugin.js'), 'utf8'), sentinel)
  await assert.rejects(fs.stat(path.join(collision, PACKAGE_MARKER)), { code: 'ENOENT' })
}
assert.equal(await fs.readFile(path.join(standalone, 'plugin.js'), 'utf8'), '// unrelated standalone\n')
console.log(`PASS: real desktop reconciliation (${phase}), ownership and unrelated installation preserved`)
