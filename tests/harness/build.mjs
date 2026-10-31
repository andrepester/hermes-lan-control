import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
const root = dirname(fileURLToPath(import.meta.url))
await build({ entryPoints: [resolve(root, 'main.js')], bundle: true, format: 'esm',
  outfile: resolve(root, 'dist/main.js'), nodePaths: [resolve(root, 'node_modules')],
  alias: { '@hermes/plugin-sdk': resolve(root, 'sdk.js') },
  define: { 'process.env.NODE_ENV': '"development"' } })
console.log('Harness gebaut: dist/main.js (derselbe desktop/plugin.js).')
