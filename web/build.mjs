import { cp, mkdir, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const outdir = fileURLToPath(new URL('../pugc_toolkit/web', import.meta.url));
await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });
await build({
  entryPoints: ['render.js'],
  bundle: true,
  minify: true,
  format: 'esm',
  target: 'es2022',
  legalComments: 'linked',
  outdir,
});
await cp(new URL("render.html", import.meta.url), `${outdir}/render.html`);
