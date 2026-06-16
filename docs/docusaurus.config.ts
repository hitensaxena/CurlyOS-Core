import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// CurlyOS Core documentation site.
// Deployed to GitHub Pages as a project site: https://hitensaxena.github.io/CurlyOS-Core/
const config: Config = {
  title: 'CurlyOS Core',
  tagline: 'A cognitive operating system for AI agents',
  favicon: 'img/favicon.svg',

  url: 'https://hitensaxena.github.io',
  baseUrl: '/CurlyOS-Core/',

  organizationName: 'hitensaxena',
  projectName: 'CurlyOS-Core',
  trailingSlash: false,

  // Cross-links between many generated docs — warn (don't fail the build) and surface them.
  onBrokenLinks: 'warn',

  markdown: {
    // Parse .md as CommonMark (not strict MDX) so literal { } and < > in the
    // generated reference docs are treated as text, not JSX expressions/tags.
    format: 'detect',
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },
  themes: ['@docusaurus/theme-mermaid'],

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl:
            'https://github.com/hitensaxena/CurlyOS-Core/tree/main/docs/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'CurlyOS Core',
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          to: '/docs/reference/api-reference',
          label: 'API',
          position: 'left',
        },
        {
          to: '/docs/reference/database-schema',
          label: 'Schema',
          position: 'left',
        },
        {
          href: 'https://github.com/hitensaxena/CurlyOS-Core',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Introduction', to: '/docs/'},
            {label: 'Architecture', to: '/docs/architecture/overview'},
            {label: 'Subsystems', to: '/docs/subsystems/memory'},
          ],
        },
        {
          title: 'Reference',
          items: [
            {label: 'REST API', to: '/docs/reference/api-reference'},
            {label: 'Database Schema', to: '/docs/reference/database-schema'},
            {label: 'Integrations', to: '/docs/integrations/hermes-and-mcp'},
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'GitHub', href: 'https://github.com/hitensaxena/CurlyOS-Core'},
            {label: 'Operations', to: '/docs/operations/deployment-and-operations'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Hiten Saxena. CurlyOS Core is MIT-licensed.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'json', 'sql', 'python', 'yaml'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
