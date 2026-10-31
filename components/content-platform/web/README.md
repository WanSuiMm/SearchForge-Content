# ContentPlatform browser UI

The editable interface is React + TypeScript under src/. It calls the
same-origin ContentPlatform API directly; it contains no fixture data or
development API proxy. The service continues to publish only /,
/assets/app.js, and /assets/styles.css.

With Node.js and npm available, run npm ci followed by npm run build.

The build type-checks the sources, bundles React into dist/app.js, then copies
that bundle to app.js, the path served by the local Python service. The
existing styles.css is linked directly by index.html and remains the UI
stylesheet.
