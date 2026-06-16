import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';

type Feature = {title: string; to: string; description: string};

const FEATURES: Feature[] = [
  {
    title: 'Multi-tier memory',
    to: '/docs/subsystems/memory',
    description:
      'Working, episodic, semantic and procedural tiers with a fast hot path and a deferred "sleep" consolidation job.',
  },
  {
    title: 'Knowledge graph',
    to: '/docs/subsystems/knowledge-graph',
    description:
      'LLM-extracted triples, entity resolution, and a densified, fully-connected typed graph — never a dust cloud of islands.',
  },
  {
    title: 'Metacognition',
    to: '/docs/subsystems/cognition',
    description:
      'Reflection, narrative, attention and meta faculties that read the clean graph and revise principles, themes and identity.',
  },
  {
    title: 'Autonomous orchestration',
    to: '/docs/subsystems/orchestration',
    description:
      'An agent loop — opportunity → goal → agent → verify → repeat — running in a sandbox under safety governance.',
  },
  {
    title: 'Bi-temporal & grounded',
    to: '/docs/architecture/concepts',
    description:
      'Every fact is time-aware, cites its source episode, and is invalidated — never deleted. Full provenance, reversible.',
  },
  {
    title: 'REST + MCP + Hermes',
    to: '/docs/reference/api-reference',
    description:
      '116 REST routes, 17 MCP tools, and a drop-in Hermes MemoryProvider plugin over 43 Postgres tables.',
  },
];

function Hero(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero', styles.hero)}>
      <div className="container">
        <Heading as="h1" className={styles.heroTitle}>
          {siteConfig.title}
        </Heading>
        <p className={styles.heroSubtitle}>{siteConfig.tagline}</p>
        <p className={styles.heroBlurb}>
          A standalone Python service that gives an AI agent a real cognitive
          substrate: structure, time, provenance and self-revision — not just
          recall.
        </p>
        <div className={styles.buttons}>
          <Link className="button button--primary button--lg" to="/docs/">
            Read the docs
          </Link>
          <Link
            className="button button--secondary button--lg"
            to="/docs/getting-started/installation">
            Get started
          </Link>
        </div>
      </div>
    </header>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title={`${siteConfig.title} — Documentation`}
      description="Documentation for CurlyOS Core, a cognitive operating system for AI agents.">
      <Hero />
      <main className="container">
        <section className={styles.features}>
          {FEATURES.map((f) => (
            <Link key={f.title} to={f.to} className={styles.featureCard}>
              <Heading as="h3">{f.title}</Heading>
              <p>{f.description}</p>
            </Link>
          ))}
        </section>
      </main>
    </Layout>
  );
}
