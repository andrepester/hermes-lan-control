import { createRoot } from 'react-dom/client'
import plugin from '../../desktop/plugin.js'
import { context, control, switchProfile, host, emitEvent } from './sdk.js'

const root = createRoot(document.getElementById('plugin'))
const settingsRoot = createRoot(document.getElementById('settings'))
let ctx
const mount = () => { ctx?.dispose(); ctx = context(node => root.render(node), node => settingsRoot.render(node)); plugin.register(ctx) }
async function configure(event) {
  const mode = document.getElementById('mode').value
  control.transport = ['backend', 'stale'].includes(mode) ? mode : 'ok'
  await fetch('/demo', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, ping: document.getElementById('ping').value, health_on: document.getElementById('health-on').checked,
      long_label: document.getElementById('long').checked, profile: host.state.profile.get() }) })
  // Network outcomes do not change configuration or cancel post-wake tracking.
  if (['health-on', 'long'].includes(event?.target?.id)) emitEvent('plugin.lan-control.settings.changed')
}
document.getElementById('mode').onchange = configure
document.getElementById('ping').onchange = configure
document.getElementById('health-on').onchange = configure
document.getElementById('long').onchange = configure
document.getElementById('theme').onclick = () => document.body.classList.toggle('light')
document.getElementById('profile').onclick = async () => { switchProfile(); await configure() }
document.getElementById('reload').onclick = mount
document.getElementById('disable').onclick = () => ctx.dispose()
document.getElementById('enable').onclick = mount
document.getElementById('narrow').onclick = () => document.getElementById('shell').classList.toggle('narrow')
setInterval(async () => {
  const data = await (await fetch('/demo')).json()
  document.getElementById('counts').textContent = `Simulierte Pings: ${data.ping_checks} · HTTP-Prüfungen: ${data.checks} · WoL-POSTs: ${control.wakeRequests} · abgefangene Pakete: ${data.packets} · Token-Speicherungen im RAM: ${data.token_writes} · Profil: ${host.state.profile.get()}`
}, 500)
await configure()
mount()
