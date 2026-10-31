import { createElement as h } from 'react'
let epoch = 0
const atom = value => {
  const listeners = new Set()
  return { get: () => value, subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn) },
    set: next => { value = next; listeners.forEach(fn => fn(value)) } }
}
export const host = { state: { profile: atom('demo-a'), connectionId: atom('loopback-demo'), gateway: atom('open') },
  notify: value => { document.getElementById('notice').textContent = value.message },
  request: async () => { throw new Error('Unexpected RPC: settings use the plugin transaction route') } }
export const Input = props => h('input', { ...props, className: 'native-input' })
export const Button = ({ variant, size, ...props }) => h('button', props)
// Exakt die benannten Native-Props; label/children werden bewusst NICHT gelesen.
export const ListRow = ({ title, description, hint, action, below }) => h('div', { className: 'settings-row' },
  h('div', null, title, description && h('p', null, description), hint, below), action)
export const STATUSBAR_AREAS = { right: 'statusBar.right' }
export const gatewayActivationEpoch = () => epoch
export const switchProfile = () => { epoch++; host.state.profile.set(host.state.profile.get() === 'demo-a' ? 'demo-b' : 'demo-a') }
export const control = { transport: 'ok', events: new Map(), wakeRequests: 0 }
export function emitEvent(name) { control.events.get(name)?.forEach(fn => fn()) }
export function context(render, renderSettings) {
  const disposers = []
  return {
    register: value => { render(value.render()); return () => render(null) },
    registerSettingsPage: value => { renderSettings(value.render()); return () => renderSettings(null) },
    rest: async (path, options = {}) => {
      if (path === '/wake') control.wakeRequests++
      if (control.transport === 'backend') throw new Error('Simulierter Backend-Ausfall')
      if (control.transport === 'stale' && path === '/status') {
        await new Promise((_, reject) => setTimeout(() => reject(new Error('Simulierter Timeout')), options.timeoutMs || 35000))
      }
      const response = await fetch('/api/plugins/lan-control' + path, {
        method: options.method || 'GET', headers: { 'Content-Type': 'application/json' },
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: AbortSignal.timeout(options.timeoutMs || 35000)
      })
      if (!response.ok) throw new Error('Backend request failed')
      const result = await response.json()
      if (path === '/settings' && options.method === 'POST' && result.outcome !== 'conflict') emitEvent('plugin.lan-control.settings.changed')
      return result
    },
    onEvent: (name, fn) => { if (!control.events.has(name)) control.events.set(name, new Set()); control.events.get(name).add(fn); return () => control.events.get(name)?.delete(fn) },
    setInterval: (fn, ms) => { const timer = setInterval(fn, ms); return () => clearInterval(timer) },
    onDispose: fn => disposers.push(fn),
    dispose: () => { disposers.forEach(fn => fn()); render(null); renderSettings(null) }
  }
}
