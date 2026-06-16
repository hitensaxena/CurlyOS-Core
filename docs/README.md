# CurlyOS Core — Documentation Site

This directory is the [Docusaurus](https://docusaurus.io/) documentation site for CurlyOS Core. The markdown content lives in [`docs/`](./docs/); the rest is site configuration.

## Local development

```bash
cd docs
npm install
npm start          # dev server at http://localhost:3000/CurlyOS-Core/
```

## Build

```bash
npm run build      # static site → docs/build/
npm run serve      # preview the production build locally
```

## Structure

```
docs/
├── docs/                       # all markdown content (the documentation)
│   ├── intro.md
│   ├── getting-started/
│   ├── architecture/
│   ├── subsystems/
│   ├── reference/              # REST API + database schema
│   ├── integrations/
│   ├── operations/
│   └── development.md
├── src/                        # homepage + custom CSS
├── static/                     # favicon, .nojekyll
├── docusaurus.config.ts        # site config
└── sidebars.ts                 # sidebar structure
```

## Deployment

A GitHub Actions workflow (`.github/workflows/docs-deploy.yml`) builds and publishes the site to GitHub Pages on every push to `main` that touches `docs/`. To enable it: repository **Settings → Pages → Build and deployment → Source: GitHub Actions**.

The site is configured for the project path `https://hitensaxena.github.io/CurlyOS-Core/` (see `baseUrl` in `docusaurus.config.ts`).
