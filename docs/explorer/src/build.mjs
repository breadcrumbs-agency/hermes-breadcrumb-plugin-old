// G03: build the explorer's bundle.js, once.
//
//   node build.mjs PIN MODULES
//
// PIN      a checkout of the plugin (read only): its desktop/plugin.js is bundled as it is, not changed.
// MODULES  a node_modules folder that holds esbuild, react, react-dom, @tanstack/react-query, @nanostores/react, nanostores
//          (the one of the Hermes Desktop checkout has them).
// Writes ../bundle.js (React inside, one copy), ../LICENSES.txt and ../BUILD.txt. Nothing else.
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = resolve(here, '..')
const [pin, modules] = process.argv.slice(2).map(p => resolve(p))
if (!pin || !modules) { console.error('usage: node build.mjs PIN MODULES'); process.exit(2) }
const sha = buf => createHash('sha256').update(buf).digest('hex')
const version = name => JSON.parse(readFileSync(join(modules, name, 'package.json'), 'utf8')).version
const real = name => realpathSync(join(modules, name))

const esbuild = createRequire(join(modules, 'x.js'))('esbuild')
const pluginFile = join(pin, 'desktop/plugin.js')
const pluginBytes = readFileSync(pluginFile)
const built = await esbuild.build({
  entryPoints: [join(here, 'entry.js')], bundle: true, format: 'iife', platform: 'browser', target: 'chrome120', absWorkingDir: here,
  outfile: join(out, 'bundle.js'), nodePaths: [modules], legalComments: 'none', logLevel: 'warning', metafile: true,
  minify: true,
  alias: { '@hermes/plugin-sdk': join(here, 'sdk.js'), 'pin-plugin': pluginFile, react: real('react'), 'react-dom': real('react-dom') },
  define: { 'process.env.NODE_ENV': '"production"' }
})
const reacts = Object.keys(built.metafile.inputs).filter(i => /\/node_modules\/react\/index\.js$/.test(i))
if (reacts.length !== 1) throw new Error(`the bundle holds ${reacts.length} copies of React`)
const bundle = readFileSync(join(out, 'bundle.js'))

// the licenses of what is inside (MIT): the bundle itself carries no comment. The list is what esbuild bundled (its
// metafile), the license file is the one in the folder of each package; a package with none stops the build.
const roots = new Map()
for (const f of Object.keys(built.metafile.inputs)) {
  const i = f.lastIndexOf('node_modules/')
  if (i < 0) continue
  const m = f.slice(i + 13).match(/^((?:@[^/]+\/)?[^/]+)\//)
  if (m) roots.set(m[1], resolve(here, f.slice(0, i + 13), m[1])) // metafile paths are relative to the working folder
}
const notices = []
const inside = []
for (const [name, dir] of [...roots].sort()) {
  const pj = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'))
  const file = readdirSync(dir).find(n => /^licen[cs]e/i.test(n))
  if (!file) throw new Error(`no license file in the folder of ${name} ${pj.version}`)
  notices.push(`== ${name} ${pj.version} (${pj.license}) ==\n${readFileSync(join(dir, file), 'utf8').trim()}\n`)
  inside.push(`${name} ${pj.version}`)
}
const pinHead = execFileSync('git', ['-C', pin, 'rev-parse', '--short', 'HEAD'], { encoding: 'utf8' }).trim()
const pinLicense = existsSync(join(pin, 'LICENSE')) ? readFileSync(join(pin, 'LICENSE'), 'utf8').trim() : ''
writeFileSync(join(out, 'LICENSES.txt'),
  'What bundle.js holds, and the licenses of it.\n\n' +
  `== hermes-breadcrumb-plugin: desktop/plugin.js at ${pinHead} (the page; MIT) ==\n${pinLicense}\n\n` + notices.join('\n') + '\n')

writeFileSync(join(out, 'BUILD.txt'), [
  'The explorer, live: how bundle.js was made (src/build.mjs).',
  '',
  `page      desktop/plugin.js of the pin ${pinHead}, ${pluginBytes.length} bytes, sha256 ${sha(pluginBytes)}. It is bundled as it is.`,
  `stand-in  src/sdk.js (the stand-in for @hermes/plugin-sdk: the names the page takes from it, and ctx.rest() answered from data/), src/entry.js (mounts the page; click, wheel, drag)`,
  `bundle.js ${bundle.length} bytes, sha256 ${sha(bundle)}; one copy of React; minified, no source map`,
  `inside    ${inside.join(', ')}`,
  `built by  esbuild ${version('esbuild')}`,
  'data/     src/make-data.py PIN data: the pin\'s demo/seed.py (profile bcdemo, fixed times), the pin\'s routes over it; stores.json has its scratch path blanked',
  'serve     from any static server (python3 -m http.server in the folder above), not by opening the file in a browser: the page reads data/ with fetch()',
  'check     node src/check.mjs',
  ''
].join('\n'))
console.log(`bundle.js ${bundle.length} bytes (${statSync(join(out, 'LICENSES.txt')).size} bytes of licenses); pin ${pinHead}; plugin.js sha256 ${sha(pluginBytes).slice(0, 12)}`)
