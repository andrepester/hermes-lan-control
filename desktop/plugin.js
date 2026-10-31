import { host, STATUSBAR_AREAS, gatewayActivationEpoch, Input, Button, ListRow } from '@hermes/plugin-sdk'
import { createElement as h, useSyncExternalStore } from 'react'

const initial = (reason = 'Not checked yet.') => ({
  label: 'LAN Control', status: 'unknown', reason, wake_allowed: false,
  revision: '', valid_for: 0, next_check_in: 5, cooldown_remaining: 0, rechecking: false
})

// Reiner Controller mit injizierbarer Uhr/SDK: dieselbe Logik läuft in Desktop und Tests.
export function createController(ctx, environment = {}) {
  const app = environment.host || host
  const epoch = environment.epoch || gatewayActivationEpoch
  const now = environment.now || (() => performance.now())
  const listeners = new Set()
  let state = initial(), scope = '', generation = 0, disposed = false
  let busy = false, waking = false, rechecking = false, freshUntil = 0, wakeUntil = 0, nextPoll = 0
  const scopeKey = () => `${epoch()}:${app.state.profile.get()}`
  const emit = value => {
    if (disposed) return
    state = value
    for (const listener of listeners) listener()
  }
  const current = (version, key) => !disposed && version === generation && key === scopeKey()
  function invalidate(reason = 'Not checked yet.') {
    generation++
    scope = scopeKey()
    busy = waking = rechecking = false
    freshUntil = wakeUntil = nextPoll = 0
    emit(initial(reason))
  }
  function adopt(value, started) {
    if (!value || !['healthy', 'degraded', 'unhealthy', 'auth_error', 'config_error', 'backend_error', 'unknown', 'stale'].includes(value.status)
        || typeof value.label !== 'string' || typeof value.reason !== 'string'
        || typeof value.revision !== 'string' || typeof value.wake_allowed !== 'boolean'
        || !Number.isFinite(value.valid_for) || value.valid_for < 0
        || !Number.isFinite(value.next_check_in) || value.next_check_in < 0
        || !Number.isFinite(value.cooldown_remaining) || value.cooldown_remaining < 0
        || (value.wake_allowed && (!Number.isFinite(value.wake_expires_at) || value.wake_expires_at <= 0
          || !Number.isFinite(value.wake_valid_for) || value.wake_valid_for < 0))) {
      throw new Error('Invalid status response')
    }
    // Request-Start statt Empfang: langsame Transporte verlängern niemals das Alter.
    freshUntil = started + Math.min(value.valid_for, 331) * 1000
    wakeUntil = started + Math.min(value.wake_valid_for || 0, 32) * 1000
    nextPoll = now() + Math.max(100, Math.min(value.next_check_in, 300) * 1000)
    if (value.wake_allowed) nextPoll = Math.min(nextPoll, Math.max(now() + 100, wakeUntil - 1000))
    // Read a fresh snapshot at cooldown expiry; elapsed transport time cannot postpone recovery.
    if (value.cooldown_remaining > 0) nextPoll = Math.min(nextPoll, Math.max(now() + 100, started + value.cooldown_remaining * 1000))
    emit({ ...value, rechecking: false, wake_allowed: ['unhealthy', 'degraded'].includes(value.status) && value.host_status === 'down' && value.wake_allowed && now() < Math.min(freshUntil, wakeUntil) })
    expire()
  }
  function expire() {
    if (['healthy', 'degraded', 'unhealthy', 'auth_error'].includes(state.status) && now() >= freshUntil) {
      emit({ ...state, status: 'stale', reason: 'Status is out of date.', wake_allowed: false })
    }
    if (state.wake_allowed && now() >= wakeUntil) emit({ ...state, wake_allowed: false })
  }
  function failed() {
    freshUntil = 0
    nextPoll = now() + 5000
    emit({ ...initial('Cannot reach the plugin backend.'), label: state.label, status: 'backend_error' })
  }
  async function poll(force = false) {
    if (disposed || busy || waking || app.state.gateway.get() !== 'open') return
    const version = generation, key = scopeKey(), started = now()
    busy = true
    try {
      const result = await ctx.rest(force ? '/recheck' : '/status', { ...(force ? { method: 'POST' } : {}), timeoutMs: 35000 })
      if (current(version, key)) adopt(result, started)
    } catch {
      if (current(version, key)) failed()
    } finally {
      if (current(version, key)) busy = false
    }
  }
  async function recheck() {
    if (disposed) return
    if (scope !== scopeKey()) invalidate('Profile or connection changed. Checking again.')
    if (app.state.gateway.get() !== 'open') { tick(); return }
    if (waking || rechecking) return
    // Ein alter Cache-GET darf den manuellen Check weder verschlucken noch
    // anschließend dessen frisches Ergebnis überschreiben.
    generation++
    const version = generation, key = scopeKey()
    busy = false
    rechecking = true
    emit({ ...state, rechecking: true, wake_allowed: false })
    try { await poll(true) }
    finally { if (current(version, key)) rechecking = false }
  }
  function tick() {
    if (disposed) return
    if (scope !== scopeKey()) invalidate('Profile or connection changed. Checking again.')
    if (app.state.gateway.get() !== 'open') {
      if (state.status !== 'backend_error') {
        invalidate('The plugin backend is disconnected.')
        emit({ ...state, status: 'backend_error' })
      }
      return
    }
    expire()
    if (now() >= nextPoll) void poll()
  }
  async function wake() {
    tick()
    if (disposed || scope !== scopeKey() || waking || rechecking || !state.wake_allowed || now() >= freshUntil) return
    // Ein laufendes GET darf den Klick nicht verschlucken oder seine Antwort später
    // über den Versandzustand schreiben. Das Backend serialisiert GET und POST.
    generation++
    busy = false
    const version = generation, key = scopeKey(), started = now(), revision = state.revision, expires_at = state.wake_expires_at
    waking = true
    emit({ ...state, wake_allowed: false, reason: 'Checking the host before sending Wake-on-LAN…' })
    try {
      const result = await ctx.rest('/wake', { method: 'POST', body: { revision, expires_at }, timeoutMs: 35000 })
      if (!current(version, key)) return
      adopt(result.state, started)
      if (result.outcome === 'sent') {
        app.notify({ kind: 'info', message: 'Wake-on-LAN packet sent to the local network. Waiting for the host to respond.' })
      } else if (result.outcome === 'send_error') {
        app.notify({ kind: 'error', message: 'Could not send the Wake-on-LAN packet.' })
      } else {
        app.notify({ kind: 'info', message: 'No Wake-on-LAN packet sent: ' + state.reason })
      }
    } catch {
      if (current(version, key)) failed()
    } finally {
      if (current(version, key)) waking = false
    }
  }
  const cleanups = [
    app.state.profile.subscribe(() => { invalidate(); tick() }),
    app.state.gateway.subscribe(tick),
    ctx.onEvent('plugin.lan-control.settings.changed', () => { invalidate('Settings changed. Checking again.'); tick() }),
    ctx.setInterval(tick, 250)
  ]
  function dispose() {
    if (disposed) return
    disposed = true
    generation++
    cleanups.forEach(cleanup => cleanup())
    listeners.clear()
  }
  ctx.onDispose(dispose)
  tick()
  return { getSnapshot: () => state, subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener) }, wake, recheck, tick, dispose }
}

const css = `
.lan-control { display:inline-flex; align-items:center; gap:5px; min-width:0; max-width:min(220px,22vw); height:100%; padding:0 5px; color:var(--ui-text-tertiary); font-family:inherit; font-size:0.6875rem; font-weight:inherit; line-height:inherit; letter-spacing:inherit; white-space:nowrap; }
.lan-control__check { appearance:none; display:inline-flex; align-items:center; gap:5px; min-width:0; height:18px; margin:0; padding:0; border:0; border-radius:3px; background:transparent; box-shadow:none; color:inherit; font:inherit; line-height:inherit; cursor:pointer; }
.lan-control__check:hover:not(:disabled) { color:var(--ui-text-primary,inherit); }
.lan-control__check:disabled { cursor:default; }
.lan-control__check:focus-visible { outline:1px solid var(--ui-text-secondary,currentColor); outline-offset:1px; }
.lan-control__dot { flex:none; width:5px; height:5px; border-radius:50%; background:var(--ui-red); }
.lan-control__dot[data-healthy="true"] { background:var(--ui-green); }
.lan-control__dot[data-degraded="true"] { background:var(--ui-yellow); }
.lan-control__label { min-width:0; overflow:hidden; text-overflow:ellipsis; }
.lan-control__power { appearance:none; display:inline-flex; align-items:center; justify-content:center; flex:none; width:20px; height:18px; margin:0; padding:2px 3px; border:0; border-radius:3px; background:transparent; box-shadow:none; color:inherit; cursor:pointer; }
.lan-control__power:hover:not(:disabled) { color:var(--ui-text-primary,inherit); background:transparent; }
.lan-control__power:disabled { color:var(--ui-text-tertiary); opacity:.4; cursor:default; }
.lan-control__power:focus-visible { outline:1px solid var(--ui-text-secondary,currentColor); outline-offset:0; }
`

export function Status({ controller }) {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  const healthy = state.status === 'healthy'
  const statusName = `${state.label}: ${healthy ? 'Checks passed' : state.status === 'degraded' ? 'Host and health checks have different results' : 'Checks have not passed'}. ${state.reason}`
  return h('span', { className: 'lan-control', title: `${state.label} — ${state.reason}` },
    h('style', null, css),
    h('button', { type: 'button', className: 'lan-control__check', 'aria-label': `${state.label}: check again`,
      'aria-busy': Boolean(state.rechecking), disabled: Boolean(state.rechecking),
      title: `${statusName} — Click to check again`, onClick: () => void controller.recheck() },
      h('span', { className: 'lan-control__dot', 'data-healthy': String(healthy), 'data-degraded': String(state.status === 'degraded'), role: 'img', 'aria-label': statusName }),
      h('span', { className: 'lan-control__label' }, state.label)),
    h('span', { title: `Wake-on-LAN — ${state.reason}`, style: { display: 'inline-flex', flex: 'none' } },
      h('button', { type: 'button', className: 'lan-control__power', 'aria-label': 'Wake-on-LAN',
        title: `Wake-on-LAN — ${state.reason}`, disabled: !state.wake_allowed, onClick: () => void controller.wake() },
      h('svg', { width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
        strokeWidth: 1.7, strokeLinecap: 'round', 'aria-hidden': true },
        h('path', { d: 'M12 3v9M6.34 5.66a8 8 0 1 0 11.32 0' })))))
}

export const settingFields = [
  ['display_name', 'Display name', 'text'], ['host', 'Host (IP address or hostname)', 'text'],
  ['wol_mac', 'Wake-on-LAN MAC address', 'text'], ['wol_broadcast', 'Broadcast address', 'text'],
  ['wol_port', 'UDP port', 'number'], ['interval', 'Check interval (seconds)', 'number'],
  ['timeout', 'Timeout (seconds)', 'number'], ['health_url', 'Health URL (optional)', 'url'],
  ['bearer_token', 'Bearer token (optional)', 'password'], ['expected_status', 'Expected HTTP status (optional)', 'text']
]

export function collectSettings(draft) {
  const values = Object.fromEntries(settingFields.filter(([key]) => key !== 'bearer_token').map(([key]) => [key, String(draft[key] ?? '').trim()]))
  if (Array.from(values.display_name).length > 200) throw new Error('Display name must be no longer than 200 characters.')
  if (!values.host || values.host.length > 253 || values.host.startsWith('-') || /[\s/\\@\x00-\x1f\x7f]/.test(values.host)) throw new Error('Enter the host to ping.')
  if (!values.host.includes(':')) {
    const parts = values.host.replace(/\.$/, '').split('.')
    const numeric = /^[0-9.]+$/.test(values.host)
    if (numeric ? parts.length !== 4 || parts.some(part => !/^(0|[1-9][0-9]{0,2})$/.test(part) || Number(part) > 255)
      : parts.some(part => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(part))) throw new Error('Enter a valid hostname for the ping target.')
  }
  for (const [key, min, max] of [['wol_port', 1, 65535], ['interval', 2, 300], ['timeout', 0.2, 30]]) {
    if (!values[key] || !Number.isFinite(Number(values[key])) || Number(values[key]) < min || Number(values[key]) > max) throw new Error(`Enter a valid number for ${key}.`)
    values[key] = Number(values[key])
  }
  if (!Number.isInteger(values.wol_port) || values.timeout >= values.interval) throw new Error('Use a whole number for the UDP port and a timeout shorter than the check interval.')
  if (values.health_url) {
    let url
    try { url = new URL(values.health_url) } catch { throw new Error('Enter a valid health URL.') }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash || url.port === '0'
      || /[\s\\]/.test(values.health_url)) throw new Error('Use an HTTP or HTTPS health URL without embedded credentials or a fragment.')
    if (values.expected_status && (!/^\d{3}$/.test(values.expected_status) || Number(values.expected_status) < 100 || Number(values.expected_status) > 599)) throw new Error('Leave the expected HTTP status blank or enter a number from 100 to 599.')
  } else delete values.expected_status // Deaktivierte gespeicherte Werte ausdrücklich erhalten.
  return values
}

export function createSettingsController(ctx, environment = {}) {
  const app = environment.host || host, epoch = environment.epoch || gatewayActivationEpoch
  const listeners = new Set()
  const identity = () => ({ connection: app.state.connectionId.get(), profile: app.state.profile.get(), epoch: epoch() })
  const key = value => JSON.stringify(value)
  let owner = identity(), generation = 0, disposed = false, revision = '', baseline = null
  let state = { owner, values: null, token: '', has_token: false, inferred_host: false, saving: false, message: '', error: false }
  const emit = next => { if (!disposed) { state = { ...state, ...next }; listeners.forEach(fn => fn()) } }
  const same = version => !disposed && version === generation && key(owner) === key(identity())
  async function reload() {
    owner = identity()
    const version = ++generation
    revision = ''; baseline = null
    emit({ owner, values: null, token: '', saving: false, message: '', error: false })
    if (app.state.gateway.get() !== 'open') return
    try {
      const result = await ctx.rest('/settings', { timeoutMs: 10000 })
      if (!same(version)) return
      baseline = Object.fromEntries(Object.entries(result.values).map(([name, value]) => [name, String(value ?? '')]))
      revision = result.revision
      emit({ values: { ...baseline }, has_token: result.has_token, inferred_host: result.inferred_host })
      return true
    } catch { if (same(version)) emit({ message: 'Could not load settings.', error: true }) }
  }
  function update(name, value) {
    if (!same(generation) || state.saving || !state.values) return
    if (name === 'bearer_token') emit({ token: value })
    else emit({ values: { ...state.values, [name]: value }, message: '', error: false })
  }
  function dirty() { return state.values && baseline && JSON.stringify(state.values) !== JSON.stringify(baseline) }
  async function save() {
    const version = generation, savedOwner = key(owner)
    if (!same(version) || state.saving || !state.values || app.state.gateway.get() !== 'open') return
    let values
    try { values = collectSettings(state.values) }
    catch (error) { emit({ message: error.message, error: true }); return }
    const tokenDraft = values.health_url ? state.token : ''
    emit({ saving: true, message: '', error: false })
    try {
      // One profile-scoped request: the backend validates, checks the revision,
      // and serializes settings, credentials, and probes under the same lock.
      const result = await ctx.rest('/settings', { method: 'POST',
        body: { revision, values, ...(tokenDraft ? { token: tokenDraft } : {}) }, timeoutMs: 35000 })
      if (!same(version)) return
      if (result?.outcome === 'conflict') {
        const loaded = await reload()
        if (loaded && key(owner) === savedOwner) emit({ message: 'Settings changed elsewhere and have been reloaded. Review them before saving.', error: true })
        return
      }
      if (!result?.ok) {
        if (result?.settings) {
          revision = result.settings.revision
          baseline = Object.fromEntries(Object.entries(result.settings.values).map(([name, value]) => [name, String(value ?? '')]))
          emit({ has_token: result.settings.has_token })
        }
        emit({ message: result?.outcome === 'invalid' && typeof result.message === 'string' ? result.message
          : result?.outcome === 'save_error' ? result.http_paused
            ? 'Save failed. HTTP checks are paused. Your draft is still here; save again to finish.'
            : 'Save failed and may be partial. Your draft is still here. Reload to check the stored settings.'
          : 'Could not save settings. Reload and try again.', error: true })
        return
      }
      const final = result.settings
      revision = final.revision
      baseline = Object.fromEntries(Object.entries(final.values).map(([name, value]) => [name, String(value ?? '')]))
      emit({ values: { ...baseline }, token: '', has_token: final.has_token, inferred_host: final.inferred_host,
        message: 'Settings saved.', error: false })
    } catch {
      if (same(version)) emit({ message: 'Could not verify the save. Reload to check the stored settings. Your draft is still here.', error: true })
    } finally { if (same(version)) emit({ saving: false }) }
  }
  const checkOwner = () => { if (key(owner) !== key(identity())) void reload() }
  const cleanups = [app.state.profile.subscribe(checkOwner), app.state.connectionId.subscribe(checkOwner),
    app.state.gateway.subscribe(() => { if (app.state.gateway.get() !== 'open') { generation++; emit({ values: null, token: '', saving: false, message: 'Backend disconnected.', error: true }) } else void reload() }),
    ctx.onEvent('plugin.lan-control.settings.changed', () => { if (!state.saving) void reload() }), ctx.setInterval(checkOwner, 250)]
  function dispose() { if (!disposed) { disposed = true; generation++; cleanups.forEach(fn => fn()); listeners.clear(); state = { ...state, token: '', values: null } } }
  ctx.onDispose(dispose)
  void reload()
  return { getSnapshot: () => state, subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn) }, update, save, reload, dirty, dispose }
}

export function SettingsPage({ controller }) {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  const values = state.values || {}, health = Boolean(values.health_url?.trim())
  const disabled = !state.values || state.saving
  return h('section', { style: { padding: 16, maxWidth: 760, color: 'var(--ui-text-primary)' } },
    h('h2', null, 'LAN Control'),
    h('p', { style: { fontSize: 12 } }, `Connected backend: ${state.owner.connection || 'disconnected'} · Profile: ${state.owner.profile}. These settings apply to the connected backend, regardless of the profile selected elsewhere in Settings.`),
    state.inferred_host && h('p', { role: 'status' }, 'The host was copied from your previous health URL. Check it and save your settings. The health URL can point to a different machine.'),
    ...settingFields.map(([name, title, type]) => h(ListRow, { key: name, title: h('label', { htmlFor: `lan-setting-${name}` }, title),
      description: name === 'health_url' ? 'Leave blank to use ping only. The health URL can point to a different machine.' : name === 'bearer_token' ? 'Leave blank to keep the saved token. Enter a new token to replace it when you save.' : name === 'expected_status' ? 'Defaults to 200 when blank. Available only when a health URL is set.' : undefined,
      action: h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 8 } },
      h(Input, { id: `lan-setting-${name}`, 'aria-label': title, type, autoComplete: 'off',
        value: name === 'bearer_token' ? state.token : values[name] ?? '',
        placeholder: name === 'bearer_token' && state.has_token ? 'Saved token — leave blank to keep it' : name === 'expected_status' ? '200' : '',
        disabled: disabled || (!health && ['bearer_token', 'expected_status'].includes(name)),
        onChange: event => controller.update(name, event.target.value), style: { maxWidth: 320, fontSize: 12 } }),
      null) })),
    h('div', { style: { display: 'flex', gap: 8, marginTop: 12 } },
      h(Button, { type: 'button', size: 'xs', disabled, onClick: () => void controller.save() }, 'Save settings'),
      h(Button, { type: 'button', size: 'xs', variant: 'outline', disabled: state.saving, onClick: () => void controller.reload() }, 'Reload')),
    h('p', { role: state.error ? 'alert' : 'status', style: { fontSize: 12 } }, state.message))
}

export default {
  id: 'lan-control',
  name: 'LAN Control',
  defaultEnabled: false,
  register(ctx) {
    const controller = createController(ctx)
    const settings = createSettingsController(ctx)
    ctx.register({ id: 'status', area: STATUSBAR_AREAS.right, order: 1000, render: () => h(Status, { controller }) })
    ctx.registerSettingsPage({ id: 'settings', title: 'LAN Control', render: () => h(SettingsPage, { controller: settings }) })
  }
}
