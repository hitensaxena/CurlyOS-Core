import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

/**
 * Explicit sidebar for the CurlyOS Core documentation.
 * Ordering and grouping are controlled here rather than auto-generated.
 */
const sidebars: SidebarsConfig = {
  docs: [
    'intro',
    {
      type: 'category',
      label: 'Getting Started',
      collapsed: false,
      items: [
        'getting-started/installation',
        'getting-started/quickstart',
        'getting-started/configuration',
      ],
    },
    {
      type: 'category',
      label: 'Architecture',
      collapsed: false,
      items: [
        'architecture/overview',
        'architecture/concepts',
        'architecture/orchestration-design-notes',
      ],
    },
    {
      type: 'category',
      label: 'Subsystems',
      collapsed: false,
      items: [
        'subsystems/memory',
        'subsystems/knowledge-graph',
        'subsystems/identity',
        'subsystems/cognition',
        'subsystems/orchestration',
        'subsystems/safety-and-governance',
        'subsystems/goals-and-workspace',
        'subsystems/studio-simulation-evaluation',
        'subsystems/shared-infrastructure',
      ],
    },
    {
      type: 'category',
      label: 'Reference',
      collapsed: false,
      items: [
        'reference/api-reference',
        'reference/database-schema',
      ],
    },
    {
      type: 'category',
      label: 'Integrations',
      items: ['integrations/hermes-and-mcp'],
    },
    {
      type: 'category',
      label: 'Operations',
      items: ['operations/deployment-and-operations'],
    },
    'development',
  ],
};

export default sidebars;
