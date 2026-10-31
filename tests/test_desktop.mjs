import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import test from 'node:test'

const context = vm.createContext({ performance, console, URL })
const sdk = new vm.SyntheticModule(['host', 'STATUSBAR_AREAS', 'gatewayActivationEpoch', 'Input', 'Button', 'ListRow'], function () {
  this.setExport('host', {})
  for (const name of ['Input', 'Button', 'ListRow']) this.setExport(name, name)
  this.setExport('STATUSBAR_AREAS', { right: 'statusBar.right' })
  this.setExport('gatewayActivationEpoch', () => 0)
}, { context })
const react = new vm.SyntheticModule(['createElement', 'useSyncExternalStore'], function () {
  this.setExport('createElement', (tag, props, ...children) => ({ tag, props: props || {}, children }))
  this.setExport('useSyncExternalStore', (_subscribe, get) => get())
}, { context })
const plugin = new vm.SourceTextModule(await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8'), { context })
await plugin.link(name => name === 'react' ? react : sdk)
await plugin.evaluate()
const { createController, Status, createSettingsController, SettingsPage, collectSettings, settingFields } = plugin.namespace
const flush = () => new Promise(resolve => setImmediate(resolve))
const reply = (status = 'unhealthy', extra = {}) => ({ label: 'demo', status, reason: 'Test',
  revision: 'r1', host_status: status === 'healthy' ? 'up' : status === 'unhealthy' ? 'down' : 'unknown', wake_allowed: status === 'unhealthy', valid_for: 8,
  wake_expires_at: 1000, wake_valid_for: 32, next_check_in: 5, cooldown_remaining: 0, ...extra })

function setup() {
  let time = 0, epoch = 0
  const calls = [], notices = [], events = new Map(), intervals = new Set(), disposers = []
  const atom = value => {
    const listeners = new Set()
    return { get: () => value, set: next => { value = next; listeners.forEach(fn => fn(value)) },
      subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn) }, listeners }
  }
  const host = { state: { profile: atom('a'), connectionId: atom('local'), gateway: atom('open') }, notify: value => notices.push(value) }
  const ctx = {
    rest: (path, options) => new Promise((resolve, reject) => calls.push({ path, options, resolve, reject })),
    onEvent: (name, fn) => { events.set(name, fn); return () => events.delete(name) },
    setInterval: fn => { intervals.add(fn); return () => intervals.delete(fn) },
    onDispose: fn => disposers.push(fn)
  }
  const environment = { host, epoch: () => epoch, now: () => time }
  const controller = createController(ctx, environment)
  return { controller, calls, host, notices, events, intervals, disposers, ctx, environment,
    advance: value => { time += value; controller.tick() },
    switchSource: () => { epoch++; controller.tick() } }
}

test('unknown/healthy/auth/config states cannot wake; status does not send POST', async () => {
  for (const status of ['healthy', 'auth_error', 'config_error', 'backend_error', 'unknown']) {
    const t = setup()
    await t.controller.wake()
    assert.equal(t.calls.length, 1)
    t.calls[0].resolve(reply(status))
    await flush()
    await t.controller.wake()
    assert.equal(t.calls.length, 1)
    assert.equal(t.controller.getSnapshot().wake_allowed, false)
  }
})

test('double click deduplicates POST and stays red after packet acknowledgement', async () => {
  const t = setup()
  t.calls[0].resolve(reply())
  await flush()
  const first = t.controller.wake()
  await t.controller.wake()
  assert.equal(t.calls.length, 2)
  assert.equal(t.calls[1].path, '/wake')
  assert.equal(t.calls[1].options.method, 'POST')
  assert.equal(t.calls[1].options.body.revision, 'r1')
  assert.equal(t.calls[1].options.body.expires_at, 1000)
  t.calls[1].resolve({ outcome: 'sent', state: reply('unhealthy', { wake_allowed: false, cooldown_remaining: 10 }) })
  await first
  assert.equal(t.controller.getSnapshot().status, 'unhealthy')
  assert.equal(t.controller.getSnapshot().wake_allowed, false)
  assert.match(t.notices[0].message, /local network/)
})

test('expired green becomes red while request is pending; no overlapping polls', async () => {
  const t = setup()
  t.calls[0].resolve(reply('healthy'))
  await flush()
  t.advance(5000)
  t.advance(3001)
  assert.equal(t.calls.length, 2)
  assert.equal(t.controller.getSnapshot().status, 'stale')
  await t.controller.wake()
  assert.equal(t.calls.length, 2)
})

test('click during refresh sends once; pending GET cannot overwrite wake state', async () => {
  const t = setup()
  t.calls[0].resolve(reply())
  await flush()
  t.advance(5000)
  assert.equal(t.calls[1].path, '/status')
  const wake = t.controller.wake()
  assert.equal(t.calls[2].path, '/wake')
  t.calls[1].resolve(reply())
  await flush()
  assert.equal(t.controller.getSnapshot().wake_allowed, false)
  await t.controller.wake()
  assert.equal(t.calls.length, 3)
  t.calls[2].resolve({ outcome: 'sent', state: reply('unhealthy', { wake_allowed: false, cooldown_remaining: 10 }) })
  await wake
  assert.equal(t.controller.getSnapshot().wake_allowed, false)
})

test('long poll intervals renew the wake expiry and an expired action cannot send', async () => {
  const t = setup()
  t.calls[0].resolve(reply('unhealthy', { valid_for: 300, next_check_in: 300 }))
  await flush()
  t.advance(31000)
  assert.equal(t.calls.length, 2)
  assert.equal(t.calls[1].path, '/status')
  t.advance(1001)
  assert.equal(t.controller.getSnapshot().status, 'unhealthy')
  assert.equal(t.controller.getSnapshot().wake_allowed, false)
  await t.controller.wake()
  assert.equal(t.calls.length, 2)
  t.calls[1].resolve(reply('unhealthy', { valid_for: 300, next_check_in: 300, wake_expires_at: 2000 }))
  await flush()
  assert.equal(t.controller.getSnapshot().wake_allowed, true)
})

test('backend failure removes green and never leaks SDK exception text', async () => {
  const t = setup()
  t.calls[0].resolve(reply('healthy'))
  await flush()
  t.advance(5000)
  t.calls[1].reject(new Error('TEST-ONLY-SECRET'))
  await flush()
  assert.equal(t.controller.getSnapshot().status, 'backend_error')
  assert.doesNotMatch(JSON.stringify(t.controller.getSnapshot()), /TEST-ONLY-SECRET/)
})

test('profile and same-profile source switches discard late responses', async () => {
  const t = setup()
  t.host.state.profile.set('b')
  t.calls[0].resolve(reply('healthy', { label: 'old' }))
  await flush()
  assert.equal(t.controller.getSnapshot().status, 'unknown')
  t.calls[1].resolve(reply('unhealthy', { label: 'new' }))
  await flush()
  t.switchSource()
  assert.equal(t.controller.getSnapshot().status, 'unknown')
  assert.equal(t.calls.length, 3)
  t.calls[2].resolve(reply('healthy', { label: 'source-b' }))
  await flush()
  assert.equal(t.controller.getSnapshot().label, 'source-b')
})

test('click before epoch timer cannot send previous target revision', async () => {
  const t = setup()
  t.calls[0].resolve(reply())
  await flush()
  t.switchSource()
  await t.controller.wake()
  assert.equal(t.calls.filter(call => call.path === '/wake').length, 0)
})

test('settings changes reset state and stale wake replies produce no toast', async () => {
  const t = setup()
  t.calls[0].resolve(reply())
  await flush()
  const wake = t.controller.wake()
  t.events.get('plugin.lan-control.settings.changed')()
  assert.equal(t.controller.getSnapshot().status, 'unknown')
  t.calls[1].resolve({ outcome: 'sent', state: reply() })
  await wake
  assert.equal(t.notices.length, 0)
  assert.equal(t.controller.getSnapshot().status, 'unknown')
})

test('disconnect disables synchronously and dispose clears all owned resources', async () => {
  const t = setup()
  t.calls[0].resolve(reply('healthy'))
  await flush()
  t.host.state.gateway.set('closed')
  assert.equal(t.controller.getSnapshot().status, 'backend_error')
  t.host.state.gateway.set('open')
  t.disposers.forEach(fn => fn())
  t.calls.at(-1).resolve(reply('healthy'))
  await flush()
  assert.equal(t.controller.getSnapshot().status, 'backend_error')
  assert.equal(t.intervals.size, 0)
  assert.equal(t.events.size, 0)
  assert.equal(t.host.state.profile.listeners.size, 0)
  assert.equal(t.host.state.gateway.listeners.size, 0)
})

test('slow transport cannot extend freshness and malformed response fails closed', async () => {
  const t = setup()
  t.advance(9000)
  t.calls[0].resolve(reply('healthy'))
  await flush()
  assert.equal(t.controller.getSnapshot().status, 'stale')
  t.advance(5000)
  t.calls[1].resolve({ status: 'healthy', wake_allowed: true })
  await flush()
  assert.equal(t.controller.getSnapshot().status, 'backend_error')
})

test('rendered contract contains only label plus accessible dot and icon button', () => {
  const tree = Status({ controller: { subscribe() {}, getSnapshot: () => reply('healthy', { label: 'Long demo name' }) } })
  assert.equal(tree.props.className, 'lan-control')
  assert.match(tree.props.title, /Long demo name/)
  const check = tree.children[1], dot = check.children[0], label = check.children[1], button = tree.children[2].children[0]
  assert.equal(check.tag, 'button')
  assert.equal(check.props['aria-label'], 'Long demo name: check again')
  assert.equal(check.props.disabled, false)
  assert.equal(dot.props['data-healthy'], 'true')
  assert.match(dot.props['aria-label'], /Checks passed/)
  assert.equal(label.children[0], 'Long demo name')
  assert.equal(button.props['aria-label'], 'Wake-on-LAN')
  assert.equal(button.props.disabled, true)
  assert.equal(button.children[0].tag, 'svg')
})

test('yellow permits wake only for confirmed down host, regardless of HTTP', async () => {
  for (const host_status of ['up', 'down', 'unknown']) {
    const t = setup()
    t.calls[0].resolve(reply('degraded', { host_status, wake_allowed: true }))
    await flush()
    assert.equal(t.controller.getSnapshot().wake_allowed, host_status === 'down')
    const tree = Status({ controller: t.controller })
    assert.equal(tree.children[1].children[0].props['data-degraded'], 'true')
  }
})

test('name and dot share manual recheck; fresh cache is bypassed and clicks deduplicate', async () => {
  const t = setup()
  t.calls[0].resolve(reply('healthy')); await flush()
  const tree = Status({ controller: t.controller })
  tree.children[1].props.onClick()
  await t.controller.recheck()
  assert.equal(t.calls.length, 2)
  assert.equal(t.calls[1].path, '/recheck')
  assert.equal(t.calls[1].options.method, 'POST')
  assert.equal(t.controller.getSnapshot().rechecking, true)
  assert.equal(Status({ controller: t.controller }).children[1].props.disabled, true)
  t.calls[1].resolve(reply()); await flush()
  assert.equal(t.controller.getSnapshot().rechecking, false)
  assert.equal(t.controller.getSnapshot().wake_allowed, true)
})

test('manual check supersedes pending GET, blocks wake, and discards old scope results', async () => {
  const t = setup()
  const check = t.controller.recheck()
  t.calls[0].resolve(reply('healthy')); await flush()
  assert.equal(t.controller.getSnapshot().status, 'unknown')
  await t.controller.wake()
  assert.equal(t.calls.length, 2)
  t.calls[1].resolve(reply()); await check
  const next = t.controller.recheck()
  t.host.state.profile.set('b')
  t.calls[2].resolve(reply('healthy', { label: 'old' })); await next
  assert.equal(t.controller.getSnapshot().status, 'unknown')
  assert.equal(t.controller.getSnapshot().rechecking, false)
})

test('manual check failure, disconnect, disposal and pending wake never send extra actions', async () => {
  const t = setup()
  t.calls[0].resolve(reply()); await flush()
  const wake = t.controller.wake()
  await t.controller.recheck()
  assert.equal(t.calls.length, 2)
  t.calls[1].resolve({ outcome: 'sent', state: reply() }); await wake
  const check = t.controller.recheck()
  t.calls[2].reject(new Error('PRIVATE-SECRET')); await check
  assert.equal(t.controller.getSnapshot().status, 'backend_error')
  assert.equal(t.controller.getSnapshot().rechecking, false)
  assert.doesNotMatch(JSON.stringify(t.controller.getSnapshot()), /PRIVATE-SECRET/)
  t.host.state.gateway.set('closed')
  await t.controller.recheck()
  assert.equal(t.calls.length, 3)
  t.controller.dispose()
  await t.controller.recheck()
  assert.equal(t.calls.length, 3)
})

const settingsReply = (extra = {}) => ({ values: { display_name: 'Demo', host: 'target.invalid', wol_mac: '02:00:00:00:00:01', wol_broadcast: '192.0.2.255', wol_port: 9,
  interval: 5, timeout: 2, health_url: 'http://localhost/health', expected_status: '' }, has_token: true, inferred_host: false, revision: 'revision-a', ...extra })
async function settingsSetup() {
  const t = setup()
  t.controller.dispose(); t.calls.length = 0
  t.rpc = []
  t.host.request = async (...args) => { t.rpc.push(args); return { ok: true } }
  t.form = createSettingsController(t.ctx, t.environment)
  t.calls[0].resolve(settingsReply())
  await flush()
  return t
}

test('settings use native ListRow title/action and ordered conditional inputs', async () => {
  const t = await settingsSetup()
  t.form.update('health_url', '')
  const tree = SettingsPage({ controller: t.form })
  const rows = tree.children.filter(node => node?.tag === 'ListRow')
  assert.deepEqual(rows.map(row => row.props.key), Array.from(settingFields, field => field[0]))
  for (const row of rows) {
    assert.equal(row.props.label, undefined)
    assert.equal(row.props.title.tag, 'label')
    assert.ok(row.props.action)
    assert.equal(row.children.length, 0)
    const input = row.props.action.children[0]
    assert.equal(input.props.id, row.props.title.props.htmlFor)
    assert.equal(input.props.disabled, ['bearer_token', 'expected_status'].includes(row.props.key))
  }
  assert.match(tree.children[1].children[0], /local.*a/)
})

test('optional HTTP save uses one revision-bound request and preserves unused fields', async () => {
  const t = await settingsSetup()
  t.form.update('health_url', '')
  t.form.update('bearer_token', 'DRAFT-SECRET')
  const saving = t.form.save()
  const call = t.calls[1]
  assert.equal(call.path, '/settings')
  assert.equal(call.options.method, 'POST')
  assert.equal(call.options.body.revision, 'revision-a')
  assert.equal(call.options.body.values.expected_status, undefined)
  assert.equal(call.options.body.token, undefined)
  call.resolve({ ok: true, settings: settingsReply({ values: { ...settingsReply().values, health_url: '' }, revision: 'new' }) })
  await saving
  assert.equal(t.form.getSnapshot().has_token, true)
  assert.equal(t.rpc.length, 0)
  assert.equal(t.calls.length, 2)
})

test('single Save sends endpoint and token together without intermediate RPC writes', async () => {
  const t = await settingsSetup()
  t.form.update('health_url', 'https://new.invalid/health')
  t.form.update('bearer_token', 'DRAFT-SECRET')
  const saving = t.form.save()
  assert.equal(t.calls[1].options.body.values.health_url, 'https://new.invalid/health')
  assert.equal(t.calls[1].options.body.token, 'DRAFT-SECRET')
  assert.equal(t.calls[1].options.body.values.bearer_token, undefined)
  assert.equal(t.calls[1].options.timeoutMs, 35000)
  t.calls[1].resolve({ ok: true, settings: settingsReply({ revision: 'new' }) })
  await saving
  assert.equal(t.form.getSnapshot().token, '')
  assert.equal(t.form.getSnapshot().message, 'Settings saved.')
  assert.equal(t.rpc.length, 0)
})

test('profile and connection switches during save discard old drafts and replies', async () => {
  for (const field of ['profile', 'connectionId']) {
    const t = await settingsSetup()
    t.form.update('bearer_token', 'A-PRIVATE-SECRET')
    const saving = t.form.save()
    t.host.state[field].set('b')
    assert.equal(t.form.getSnapshot().token, '')
    t.calls[2].resolve(settingsReply({ revision: 'b' })); await flush()
    t.calls[1].resolve({ ok: true, settings: settingsReply({ revision: 'a-new' }) })
    await saving
    assert.equal(t.form.getSnapshot().owner[field === 'profile' ? 'profile' : 'connection'], 'b')
    assert.equal(t.form.getSnapshot().message, '')
    assert.equal(t.form.getSnapshot().token, '')
    assert.equal(t.calls.length, 3)
  }
})

test('conflicting save reloads authoritative values and reports the conflict', async () => {
  const t = await settingsSetup()
  t.form.update('display_name', 'My draft')
  const saving = t.form.save()
  t.calls[1].resolve({ ok: false, outcome: 'conflict' }); await flush()
  t.calls[2].resolve(settingsReply({ values: { ...settingsReply().values, display_name: 'Other window' }, revision: 'other' }))
  await saving
  assert.equal(t.form.getSnapshot().values.display_name, 'Other window')
  assert.match(t.form.getSnapshot().message, /Settings changed elsewhere/)
  assert.equal(t.form.getSnapshot().error, true)
})

test('profile switch during conflict reload cannot leak conflict notices or drafts', async () => {
  const t = await settingsSetup()
  const saving = t.form.save()
  t.calls[1].resolve({ ok: false, outcome: 'conflict' }); await flush()
  t.host.state.profile.set('b')
  t.calls[3].resolve(settingsReply({ revision: 'b' })); await flush()
  t.calls[2].resolve(settingsReply())
  await saving
  assert.equal(t.form.getSnapshot().owner.profile, 'b')
  assert.equal(t.form.getSnapshot().message, '')
})

test('partial save keeps draft and retries with the returned stored revision', async () => {
  const t = await settingsSetup()
  t.form.update('health_url', 'https://new.invalid/health')
  t.form.update('bearer_token', 'DRAFT-SECRET')
  const saving = t.form.save()
  t.calls[1].resolve({ ok: false, outcome: 'save_error', http_paused: true,
    settings: settingsReply({ values: { ...settingsReply().values, health_url: '' }, revision: 'partial' }) })
  await saving
  assert.equal(t.form.getSnapshot().token, 'DRAFT-SECRET')
  assert.equal(t.form.getSnapshot().values.health_url, 'https://new.invalid/health')
  assert.match(t.form.getSnapshot().message, /HTTP checks are paused/)
  assert.equal(t.form.getSnapshot().saving, false)
  const retry = t.form.save()
  assert.equal(t.calls[2].options.body.revision, 'partial')
  t.calls[2].resolve({ ok: true, settings: settingsReply({ revision: 'done' }) })
  await retry
  assert.equal(t.form.getSnapshot().token, '')
})

test('validation failures retain drafts and never show success', async () => {
  const t = await settingsSetup()
  t.form.update('host', '::invalid') // Backend is authoritative for IPv6 validation.
  const saving = t.form.save()
  t.calls[1].resolve({ ok: false, outcome: 'invalid', message: 'Enter a valid host to ping.' })
  await saving
  assert.equal(t.form.getSnapshot().values.host, '::invalid')
  assert.equal(t.form.getSnapshot().message, 'Enter a valid host to ping.')
  assert.equal(t.form.getSnapshot().error, true)
})

test('malformed hosts, long names, and invalid URLs fail before a write', async () => {
  for (const changes of [{ host: 'a..b' }, { host: 'a.-b' }, { host: '127.0.0.999' }, { host: '' },
    { display_name: 'x'.repeat(201) }, { health_url: 'http://localhost:0' }]) {
    const t = await settingsSetup()
    for (const [name, value] of Object.entries(changes)) t.form.update(name, value)
    await t.form.save()
    assert.equal(t.calls.length, 1)
    assert.equal(t.form.getSnapshot().error, true)
  }
})

test('transport failure preserves drafts without echoing a secret exception', async () => {
  const t = await settingsSetup()
  t.form.update('bearer_token', 'DRAFT-SECRET')
  const saving = t.form.save()
  t.calls[1].reject(new Error('DRAFT-SECRET'))
  await saving
  assert.equal(t.form.getSnapshot().token, 'DRAFT-SECRET')
  assert.doesNotMatch(t.form.getSnapshot().message, /DRAFT-SECRET/)
  assert.match(t.form.getSnapshot().message, /Could not verify/)
})
