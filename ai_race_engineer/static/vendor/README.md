# Vendored browser runtime

`vue-3.5.41.global.prod.js` is `dist/vue.global.prod.js` from the published
`vue@3.5.41` npm package:

- Tarball: <https://registry.npmjs.org/vue/-/vue-3.5.41.tgz>
- npm integrity: `sha512-2laE0p+aK+/AOPG/XL/WepOs/GlK755LJ1XECi9kDUrz1FKNw8rb2Xzlw9JS1rqEV55nb0ttsKxVlTCcd+R5cg==`
- License: MIT; reproduced in `VUE-LICENSE.txt`

To update it, inspect the new package, change the versioned filename and HTML
reference together, retain the license, and run `tests/test_release_packaging.py`.

## Lucide icons

The panel's icons are path data from `lucide-static@0.460.0`
(<https://unpkg.com/lucide-static@0.460.0/icons/>), inlined as `<symbol>`s at
the top of `panel.html` so the panel needs no network. License: ISC;
reproduced in `LUCIDE-LICENSE.txt`.
