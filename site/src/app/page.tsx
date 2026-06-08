'use client';
import { useState, useEffect } from 'react';
import { A } from '../components/tokens';
import { Reveal } from '../components/Reveal';
import { ScrollNav } from '../components/Nav';
import { Footer } from '../components/Footer';
import { DagAnimation } from '../components/DagAnimation';

const SERIF = "'Newsreader', var(--font-newsreader), Georgia, serif";
const SANS  = "'DM Sans', var(--font-dm-sans), system-ui, sans-serif";
const MONO  = "'IBM Plex Mono', var(--font-ibm-plex-mono), monospace";

// ─── HERO ─────────────────────────────────────────────────────────────────────
function Hero() {
  const [loaded, setLoaded] = useState(false);
  useEffect(() => { setTimeout(() => setLoaded(true), 100); }, []);

  return (
    <section style={{
      backgroundColor: A.bg,
      minHeight: '88vh',
      display: 'flex',
      alignItems: 'center',
      position: 'relative',
      overflow: 'hidden',
    }}>
      {/* Ambient glow */}
      <div style={{
        position: 'absolute', top: 0, right: '8%',
        width: 600, height: 600,
        background: 'radial-gradient(ellipse, rgba(74,222,128,0.05) 0%, transparent 65%)',
        pointerEvents: 'none',
      }} />
      <div style={{
        position: 'absolute', bottom: 0, left: '5%',
        width: 400, height: 400,
        background: 'radial-gradient(ellipse, rgba(96,165,250,0.04) 0%, transparent 65%)',
        pointerEvents: 'none',
      }} />

      <div style={{
        maxWidth: 1060, margin: '0 auto', padding: '120px 32px 100px',
        display: 'flex', alignItems: 'center', gap: 72, width: '100%',
      }}>
        {/* Left: text */}
        <div style={{
          flex: '0 0 520px',
          opacity: loaded ? 1 : 0,
          transform: loaded ? 'translateY(0)' : 'translateY(24px)',
          transition: 'all 0.8s ease',
        }}>
          {/* Badge */}
          <div style={{
            display: 'inline-flex', alignItems: 'center', gap: 8,
            padding: '5px 14px', marginBottom: 32,
            backgroundColor: 'rgba(74,222,128,0.07)',
            border: `1px solid rgba(74,222,128,0.22)`,
            borderRadius: 999,
          }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', backgroundColor: A.researcher }} />
            <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.researcher, letterSpacing: '0.04em' }}>
              Open Source · Python 3.11+ · MIT License
            </span>
          </div>

          {/* Headline */}
          <h1 style={{
            fontFamily: SERIF, fontSize: 56, fontWeight: 400,
            color: A.textBright, lineHeight: 1.1, margin: '0 0 8px',
            letterSpacing: '-0.02em',
          }}>
            A maturity
          </h1>
          <h1 style={{
            fontFamily: SERIF, fontSize: 56, fontWeight: 400,
            color: A.researcher, lineHeight: 1.1, margin: '0 0 28px',
            letterSpacing: '-0.02em', fontStyle: 'italic',
          }}>
            of AI agents.
          </h1>

          <p style={{
            fontFamily: SANS, fontSize: 17, color: A.textSecondary,
            lineHeight: 1.75, margin: '0 0 14px', maxWidth: 480,
          }}>
            Not just a collective noun — a design principle.
          </p>

          <p style={{
            fontFamily: SANS, fontSize: 15, color: A.textMuted,
            lineHeight: 1.75, margin: '0 0 44px', maxWidth: 460,
          }}>
            Armature is a YAML-first multi-agent workflow harness. Define researcher,
            worker, and judge agents. Execute them as a DAG. Then let the system study
            its own traces and rewrite its own specification — every run, every time.
          </p>

          <div style={{ display: 'flex', gap: 14, alignItems: 'center' }}>
            <a
              href="https://github.com/bryansparks/armature"
              target="_blank"
              rel="noopener noreferrer"
              style={{
                fontFamily: SANS, fontSize: 15, fontWeight: 600,
                color: A.bg, backgroundColor: A.researcher,
                padding: '14px 32px', borderRadius: 8, textDecoration: 'none',
                boxShadow: '0 2px 16px rgba(74,222,128,0.25)',
              }}
            >Star on GitHub →</a>
            <a href="#how-it-works" style={{
              fontFamily: SANS, fontSize: 15, fontWeight: 500,
              color: A.textSecondary, padding: '14px 20px', textDecoration: 'none',
            }}>How it works ↓</a>
          </div>

          {/* Subtext */}
          <div style={{ marginTop: 48 }}>
            <p style={{
              fontFamily: SERIF, fontSize: 14, fontStyle: 'italic',
              color: A.textMuted, lineHeight: 1.65,
            }}>
              Like a murder of crows is more dangerous than one,<br />
              a maturity of agents is smarter than before.
            </p>
          </div>
        </div>

        {/* Right: animated DAG */}
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          opacity: loaded ? 1 : 0,
          transition: 'opacity 1s ease 0.4s',
          position: 'relative',
        }}>
          <div style={{
            position: 'absolute',
            width: 360, height: 360,
            background: 'radial-gradient(ellipse, rgba(74,222,128,0.06) 0%, rgba(96,165,250,0.04) 45%, transparent 70%)',
            borderRadius: '50%',
            pointerEvents: 'none',
          }} />
          <DagAnimation size={320} />
        </div>
      </div>
    </section>
  );
}

// ─── THE NAME ─────────────────────────────────────────────────────────────────
function TheMaturity() {
  return (
    <section style={{ backgroundColor: A.surface, borderTop: `1px solid ${A.border}`, borderBottom: `1px solid ${A.border}` }}>
      <div style={{ maxWidth: 800, margin: '0 auto', padding: '88px 32px', textAlign: 'center' }}>
        <Reveal>
          <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
            Why "a maturity"?
          </span>
          <h2 style={{
            fontFamily: SERIF, fontSize: 40, fontWeight: 400,
            color: A.textBright, lineHeight: 1.25, margin: '20px 0 8px',
          }}>
            Birds flock. Geese gaggle.<br />Crows murder.
          </h2>
          <h2 style={{
            fontFamily: SERIF, fontSize: 40, fontWeight: 400,
            color: A.researcher, lineHeight: 1.25, margin: '0 0 36px',
            fontStyle: 'italic',
          }}>
            AI agents mature.
          </h2>
        </Reveal>

        <Reveal delay={0.1}>
          <p style={{
            fontFamily: SANS, fontSize: 16, color: A.textSecondary,
            lineHeight: 1.8, margin: '0 0 20px',
          }}>
            Every collective noun for animals captures something true about how they
            move and behave together. A <em style={{ color: A.text }}>murder</em> of crows
            isn&apos;t just a group — it names the coordinated, intelligent behavior that makes
            them formidable as a collective. We chose <em style={{ color: A.text }}>maturity</em> deliberately.
          </p>
          <p style={{
            fontFamily: SANS, fontSize: 16, color: A.textSecondary,
            lineHeight: 1.8, margin: '0 0 40px',
          }}>
            Armature&apos;s agents don&apos;t just coordinate to complete a task. After every run,
            the system collects execution traces, runs the DiagnosticAnalyzer against them,
            and uses the SpecRefiner to rewrite the YAML sections that underperformed. The
            next run is better. Your workflow doesn&apos;t just run.{' '}
            <span style={{ color: A.textBright, fontWeight: 500 }}>It matures.</span>
          </p>
        </Reveal>

        <Reveal delay={0.2}>
          <div style={{
            padding: '24px 32px',
            backgroundColor: 'rgba(74,222,128,0.05)',
            border: `1px solid rgba(74,222,128,0.15)`,
            borderRadius: 10,
          }}>
            <p style={{
              fontFamily: SERIF, fontSize: 18, fontStyle: 'italic',
              color: A.text, lineHeight: 1.7, margin: 0,
            }}>
              &ldquo;A murder of crows is more dangerous than one.<br />
              A maturity of agents is smarter than before.&rdquo;
            </p>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

// ─── HOW IT WORKS ─────────────────────────────────────────────────────────────
function HowItWorks() {
  const steps = [
    {
      num: '01', label: 'SPECIFY', color: A.researcher,
      title: 'Write a YAML spec. That\'s it.',
      body: 'Define your agents by role, model tier, and dependencies. Armature validates the DAG before the first run — catching cycles, missing deps, and misconfigured stages. No framework to learn. No graph API to wire.',
      detail: [
        'role: researcher | worker | judge | orchestrator',
        'tier: small | medium | large (maps to your model config)',
        'depends_on: [list of upstream stage IDs]',
        'output_mode: text | guided_json with schema validation',
      ],
    },
    {
      num: '02', label: 'EXECUTE', color: A.worker,
      title: 'DAG execution. Context flows automatically.',
      body: 'Independent stages run in parallel. Dependent stages wait for their inputs. Every stage receives the full accumulated context from all upstream stages — no wiring, no passing variables by hand. One shared dict, built up as the workflow runs.',
      detail: [
        'Parallel fan-out for independent branches',
        'Context dict accumulates all upstream outputs',
        'guided_json with automatic tier escalation on failure',
        'Checkpoint & resume — survive crashes mid-workflow',
      ],
    },
    {
      num: '03', label: 'IMPROVE', color: A.judge,
      title: 'The workflow rewrites itself.',
      body: 'Every run generates a trace. The SelfImproveRunner computes IHR across all stages, identifies which ones drag the score down, and rewrites targeted YAML sections. Add --auto-improve to any run and Armature applies safe fixes automatically — or stages structural rewrites for human review. The next run is better. Verifiably.',
      detail: [
        'IHR = 0.35×valid_rate + 0.25×success + 0.20×quorum + 0.10×latency + 0.10×happy_path',
        'DiagnosticAnalyzer identifies the lowest-scoring stages',
        'SpecRefiner rewrites only the underperforming YAML sections',
        'Prediction-verification: fixes are confirmed or flagged each cycle',
      ],
    },
  ];

  return (
    <section id="how-it-works" style={{ padding: '88px 32px', maxWidth: 1060, margin: '0 auto' }}>
      <Reveal>
        <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
          How It Works
        </span>
        <h2 style={{
          fontFamily: SERIF, fontSize: 36, fontWeight: 400,
          color: A.textBright, lineHeight: 1.25, margin: '12px 0 12px',
        }}>
          Three steps. One cycle.
          <br /><span style={{ color: A.textSecondary, fontStyle: 'italic' }}>Spec. Execute. Improve.</span>
        </h2>
        <p style={{
          fontFamily: SANS, fontSize: 16, color: A.textSecondary,
          lineHeight: 1.7, margin: '0 0 48px', maxWidth: 560,
        }}>
          Armature isn&apos;t a run-once tool. It&apos;s a loop. Each step feeds the next,
          and the next run is smarter than the last.
        </p>
      </Reveal>

      {steps.map((step, i) => (
        <Reveal key={i} delay={i * 0.1}>
          <div style={{
            display: 'flex', gap: 28, padding: '36px',
            backgroundColor: A.surface, border: `1px solid ${A.border}`,
            borderRadius: 12, marginBottom: 20, position: 'relative', overflow: 'hidden',
          }}>
            <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: 4, backgroundColor: step.color, opacity: 0.7 }} />

            <div style={{ flexShrink: 0, width: 60 }}>
              <div style={{
                width: 52, height: 52, borderRadius: 12,
                backgroundColor: `${step.color}10`,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                <span style={{ fontFamily: MONO, fontSize: 16, fontWeight: 700, color: step.color }}>{step.num}</span>
              </div>
              <div style={{
                fontFamily: MONO, fontSize: 9, color: step.color, fontWeight: 600,
                marginTop: 8, textAlign: 'center', textTransform: 'uppercase', letterSpacing: '0.06em',
              }}>{step.label}</div>
            </div>

            <div style={{ flex: 1 }}>
              <h3 style={{
                fontFamily: SERIF, fontSize: 22, fontWeight: 500,
                color: A.textBright, margin: '0 0 12px',
              }}>{step.title}</h3>
              <p style={{
                fontFamily: SANS, fontSize: 15, color: A.textSecondary,
                lineHeight: 1.7, margin: '0 0 20px',
              }}>{step.body}</p>
              <div style={{
                padding: '14px 18px', backgroundColor: A.surfaceRaised,
                borderRadius: 8, border: `1px solid ${A.border}`,
              }}>
                {step.detail.map((d, j) => (
                  <div key={j} style={{
                    display: 'flex', alignItems: 'flex-start', gap: 10,
                    marginBottom: j < step.detail.length - 1 ? 7 : 0,
                  }}>
                    <span style={{ color: step.color, fontSize: 10, marginTop: 4, flexShrink: 0 }}>▸</span>
                    <span style={{ fontFamily: MONO, fontSize: 12, color: A.textSecondary, lineHeight: 1.5 }}>{d}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </Reveal>
      ))}
    </section>
  );
}

// ─── THE THREE ROLES ──────────────────────────────────────────────────────────
function ThreeRoles() {
  const roles = [
    {
      color: A.researcher, dim: A.researcherDim, label: 'Researcher',
      glyph: '◎',
      tagline: 'Gathers.',
      body: 'The information foundation. Researchers query tools, read context, search external sources, and build the knowledge base that downstream agents draw from. They run first — and in parallel when independent.',
      examples: [
        'Market signal aggregation', 'Competitor analysis',
        'Evidence synthesis across sources', 'Tool call fan-out',
      ],
    },
    {
      color: A.worker, dim: A.workerDim, label: 'Worker',
      glyph: '◈',
      tagline: 'Transforms.',
      body: 'The production engine. Workers synthesize research into drafts, summaries, reports, code, or structured data. They consume upstream researcher output and produce the artifacts that judges and downstream workers will evaluate.',
      examples: [
        'Draft generation', 'Data transformation',
        'Code synthesis', 'Report writing',
      ],
    },
    {
      color: A.judge, dim: A.judgeDim, label: 'Judge',
      glyph: '◉',
      tagline: 'Evaluates.',
      body: 'The quality gate. Judges score output quality, validate against criteria, flag hallucinations, and decide whether a result meets the bar. Only judges contribute to the quorum score in the IHR — they are the accountability layer.',
      examples: [
        'Output quality scoring (0–10)', 'Hallucination detection',
        'Criteria validation', 'Structured pass/fail decisions',
      ],
    },
    {
      color: '#60A5FA', dim: 'rgba(96,165,250,0.1)', label: 'Orchestrator',
      glyph: '⬢',
      tagline: 'Coordinates.',
      body: 'The control plane. Orchestrators manage multi-stage execution pipelines, route work to specialized subteams, handle branching and conditional logic, and ensure all dependencies are satisfied before downstream stages run.',
      examples: [
        'Multi-stage routing', 'Parallel fan-out coordination',
        'Conditional branching', 'Error recovery & retry logic',
      ],
    },
  ];

  return (
    <section id="roles" style={{
      backgroundColor: A.surface,
      borderTop: `1px solid ${A.border}`,
      padding: '88px 32px',
    }}>
      <div style={{ maxWidth: 1060, margin: '0 auto' }}>
        <Reveal>
          <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
            Agent Roles
          </span>
          <h2 style={{
            fontFamily: SERIF, fontSize: 36, fontWeight: 400,
            color: A.textBright, lineHeight: 1.25, margin: '12px 0 12px',
          }}>
            Four roles. Every agent has one.
          </h2>
          <p style={{
            fontFamily: SANS, fontSize: 16, color: A.textSecondary,
            lineHeight: 1.7, margin: '0 0 48px', maxWidth: 580,
          }}>
            Roles aren&apos;t a label — they determine execution order, context access, and contribution
            to the self-improvement health score. A well-designed maturity uses all four.
          </p>
        </Reveal>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 20 }}>
          {roles.map((role, i) => (
            <Reveal key={i} delay={i * 0.1}>
              <div style={{
                padding: '32px 28px', height: '100%',
                backgroundColor: A.bg, border: `1px solid ${A.border}`,
                borderRadius: 12, borderTop: `3px solid ${role.color}`,
              }}>
                <div style={{
                  fontSize: 28, color: role.color, marginBottom: 16,
                  opacity: 0.8,
                }}>{role.glyph}</div>
                <div style={{
                  fontFamily: MONO, fontSize: 10, fontWeight: 700,
                  color: role.color, textTransform: 'uppercase',
                  letterSpacing: '0.1em', marginBottom: 4,
                }}>{role.label}</div>
                <h3 style={{
                  fontFamily: SERIF, fontSize: 28, fontWeight: 400,
                  color: A.textBright, margin: '0 0 16px',
                }}>{role.tagline}</h3>
                <p style={{
                  fontFamily: SANS, fontSize: 14, color: A.textSecondary,
                  lineHeight: 1.7, margin: '0 0 20px',
                }}>{role.body}</p>
                <div style={{
                  padding: '12px 14px', borderRadius: 6,
                  backgroundColor: role.dim,
                  border: `1px solid ${role.color}18`,
                }}>
                  <div style={{ fontFamily: MONO, fontSize: 9, fontWeight: 600, color: role.color, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8 }}>
                    Common uses
                  </div>
                  {role.examples.map((ex, j) => (
                    <div key={j} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: j < role.examples.length - 1 ? 5 : 0 }}>
                      <span style={{ width: 4, height: 4, borderRadius: '50%', backgroundColor: role.color, flexShrink: 0 }} />
                      <span style={{ fontFamily: SANS, fontSize: 12, color: A.textSecondary }}>{ex}</span>
                    </div>
                  ))}
                </div>
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}

// ─── SELF-IMPROVEMENT ─────────────────────────────────────────────────────────
function SelfImprovement() {
  const cycle = [
    { step: '01', label: 'Run & Trace', desc: 'Every workflow run generates a structured trace — inputs, outputs, scores, latencies, and errors per stage.', color: A.researcher },
    { step: '02', label: 'Diagnose', desc: 'DiagnosticAnalyzer computes IHR and identifies stages with the lowest per-metric contribution.', color: A.worker },
    { step: '03', label: 'Rewrite', desc: 'SpecRefiner (an LLM) receives the underperforming stage spec and rewrites the system prompt, output schema, or parameters.', color: A.judge },
    { step: '04', label: 'Verify', desc: 'The next run\'s IHR is compared to predictions. SpecRefiner tracks which fixes held and which missed — so it improves its own rewrites too.', color: A.researcher },
  ];

  return (
    <section id="improve" style={{ backgroundColor: A.bg, borderTop: `1px solid ${A.border}`, padding: '88px 32px' }}>
      <div style={{ maxWidth: 1060, margin: '0 auto' }}>
        <Reveal>
          <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.judge, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
            The Differentiator
          </span>
          <h2 style={{
            fontFamily: SERIF, fontSize: 36, fontWeight: 400,
            color: A.textBright, lineHeight: 1.25, margin: '12px 0 12px',
          }}>
            Static orchestration is table stakes.
            <br /><span style={{ color: A.judge, fontStyle: 'italic' }}>Armature learns.</span>
          </h2>
          <p style={{
            fontFamily: SANS, fontSize: 16, color: A.textSecondary,
            lineHeight: 1.7, margin: '0 0 16px', maxWidth: 600,
          }}>
            AWS AgentCore, LangGraph, and CrewAI let you build agent workflows. Armature
            does that too — and then automatically improves them across runs using the
            Implicit Harness Rating loop.
          </p>
        </Reveal>

        {/* IHR Formula */}
        <Reveal delay={0.1}>
          <div style={{
            padding: '28px 32px', margin: '36px 0',
            backgroundColor: A.surface, border: `1px solid ${A.judge}25`,
            borderRadius: 12, borderLeft: `4px solid ${A.judge}`,
          }}>
            <div style={{ fontFamily: MONO, fontSize: 10, fontWeight: 600, color: A.judge, textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 14 }}>
              Implicit Harness Rating (IHR)
            </div>
            <div style={{ fontFamily: MONO, fontSize: 18, color: A.textBright, lineHeight: 1.6 }}>
              IHR = <span style={{ color: A.researcher }}>0.35 × valid_rate</span>
              {' + '}<span style={{ color: A.worker }}>0.25 × success_rate</span>
              {' + '}<span style={{ color: A.judge }}>0.20 × avg_quorum</span>
              {' + '}<span style={{ color: A.textSecondary }}>0.10 × latency_score</span>
              {' + '}<span style={{ color: A.researcher }}>0.10 × happy_path_rate</span>
            </div>
            <div style={{ fontFamily: SANS, fontSize: 13, color: A.textMuted, marginTop: 10, lineHeight: 1.5 }}>
              Scored 0–1.0 per run. SpecRefiner targets stages whose contribution drops the overall IHR.
            </div>
          </div>
        </Reveal>

        {/* The cycle */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 36 }}>
          {cycle.map((c, i) => (
            <Reveal key={i} delay={0.1 + i * 0.07}>
              <div style={{
                padding: '24px', height: '100%',
                backgroundColor: A.surface, border: `1px solid ${A.border}`,
                borderRadius: 10, display: 'flex', gap: 16,
              }}>
                <div style={{
                  width: 36, height: 36, borderRadius: 8, flexShrink: 0,
                  backgroundColor: `${c.color}12`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}>
                  <span style={{ fontFamily: MONO, fontSize: 12, fontWeight: 700, color: c.color }}>{c.step}</span>
                </div>
                <div>
                  <div style={{ fontFamily: SANS, fontSize: 14, fontWeight: 600, color: A.textBright, marginBottom: 6 }}>{c.label}</div>
                  <div style={{ fontFamily: SANS, fontSize: 13, color: A.textSecondary, lineHeight: 1.6 }}>{c.desc}</div>
                </div>
              </div>
            </Reveal>
          ))}
        </div>

        <Reveal delay={0.4}>
          <div style={{
            padding: '20px 24px',
            backgroundColor: 'rgba(192,132,252,0.05)',
            border: `1px solid rgba(192,132,252,0.15)`,
            borderRadius: 10,
          }}>
            <p style={{
              fontFamily: SERIF, fontSize: 16, fontStyle: 'italic',
              color: A.text, lineHeight: 1.7, margin: 0,
            }}>
              Prediction-verification closes the loop: SpecRefiner declares what it expects each rewrite to fix.
              The subsequent run confirms whether the fixes held — and which ones missed. The rewriter
              improves its own judgment over time.
            </p>
          </div>
        </Reveal>

        <Reveal delay={0.5}>
          <div style={{ marginTop: 28 }}>
            <div style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 12 }}>
              Auto self-improvement — zero manual steps
            </div>
            <div style={{
              padding: '20px 24px',
              backgroundColor: A.surface, border: `1px solid ${A.border}`,
              borderRadius: 10,
            }}>
              <div style={{ fontFamily: MONO, fontSize: 13, color: A.judge, marginBottom: 10 }}>
                armature run my-workflow.yaml --auto-improve
              </div>
              <p style={{ fontFamily: SANS, fontSize: 14, color: A.textSecondary, lineHeight: 1.6, margin: 0 }}>
                Add <code style={{ color: A.judge }}>--auto-improve</code> to any run. When IHR drops below 0.75,
                Armature automatically calls SpecRefiner after execution — rewriting prompts, relaxing schemas,
                rebalancing model tiers, or tuning retry limits. Safe changes apply immediately; structural
                rewrites stage to <code style={{ color: A.textMuted }}>{'{spec}.pending.yaml'}</code> for human review.
              </p>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

// ─── RESEARCH FOUNDATION ──────────────────────────────────────────────────────
function ResearchFoundation() {
  const papers = [
    {
      num: '01', date: 'Mar 2026', source: 'Tsinghua University',
      title: 'Natural-Language Agent Harnesses',
      arxiv: '2603.25723',
      link: 'https://arxiv.org/abs/2603.25723',
      insight: 'Workflows defined in structured natural language outperform equivalent code-based harnesses — and can be reasoned about and rewritten by an optimizer.',
      gives: ['YAML spec format & DAG executor', 'Four role types (researcher/worker/judge/orchestrator)', 'IHR quality metric & parallel fan-out'],
      color: A.researcher,
    },
    {
      num: '02', date: 'Mar 2026', source: 'Stanford University',
      title: 'Meta-Harness: Automated Optimization',
      arxiv: '2603.28052',
      link: 'https://arxiv.org/abs/2603.28052',
      insight: 'Giving a frontier model access to full execution traces — not just pass/fail scores — enables causal reasoning about why runs failed and how to fix them.',
      gives: ['`armature optimize` command', 'A/B spec testing by IHR', 'Multi-iteration optimizer with proposal history'],
      color: A.worker,
    },
    {
      num: '03', date: 'Feb 2026', source: 'arXiv:2603.03329',
      title: 'AutoHarness: LLM-Synthesized Harnesses',
      arxiv: '2603.03329',
      link: 'https://arxiv.org/abs/2603.03329',
      insight: 'LLMs can generate, run, evaluate, and refine their own harness specs — producing systems that outperform larger models running without a harness.',
      gives: ['`armature new` spec wizard', 'NL → YAML synthesis loop', 'Prompt bootstrapping from trace examples'],
      color: A.judge,
    },
    {
      num: '04', date: 'Mar 2025', source: 'arXiv:2503.18666',
      title: 'AgentSpec: Runtime Safety Enforcement',
      arxiv: '2503.18666',
      link: 'https://arxiv.org/abs/2503.18666',
      insight: 'Safety constraints should be declarative rules co-located with the workflow spec — not hardcoded logic — so they can be audited, reasoned about, and generated by LLMs.',
      gives: ['Declarative `safety_rules` YAML DSL', 'Pre/post-stage and pre/post-tool hooks', '`ToolBlocked` non-retryable exception'],
      color: A.researcher,
    },
    {
      num: '05', date: 'May 2026', source: 'arXiv:2605.09998',
      title: 'Continual Harness: Reset-Free Self-Improvement',
      arxiv: '2605.09998',
      link: 'https://arxiv.org/abs/2605.09998',
      insight: 'Agentic systems can improve continuously — without human intervention or new training runs — using a two-loop design: in-run adaptation and cross-run spec refinement.',
      gives: ['`post_run` in-run refiner stage', '`armature improve` outer self-improvement loop', 'Trace export for SFT/DPO fine-tuning'],
      color: A.worker,
    },
    {
      num: '06', date: 'Apr 2026', source: 'arXiv:2604.25850',
      title: 'AHE: Observability-Driven Automatic Evolution',
      arxiv: '2604.25850',
      link: 'https://arxiv.org/abs/2604.25850',
      insight: 'Every improvement proposal must declare what it predicts it will fix — and the next cycle must verify those predictions. "Did the score go up?" is not enough.',
      gives: ['Prediction-verification loop per improvement cycle', '`predicted_fixes` / `verified_fixes` tracking', 'Falsifiable contracts on every spec revision'],
      color: A.judge,
    },
    {
      num: '07', date: 'May 2026', source: 'arXiv:2605.26112',
      title: 'From Model Scaling to System Scaling',
      arxiv: '2605.26112',
      link: 'https://arxiv.org/abs/2605.26112',
      insight: 'Three system-level failure modes that model size alone cannot fix: stale memory reaching LLMs without warning, context values flowing without provenance, and tool side effects going unverified.',
      gives: ['Memory staleness detection + `_stale_memory_keys` injection', 'Context provenance tracking per trace key', 'Post-condition verification for tool side effects', 'Drift score + component governance classification'],
      color: A.researcher,
    },
    {
      num: 'AGT', date: '2025', source: 'Microsoft',
      title: 'Agent Governance Toolkit',
      arxiv: '',
      link: 'https://github.com/microsoft/agent-governance-toolkit',
      insight: 'Production agents require auditable governance primitives baked into the execution layer — not bolted on as policy checks. Reversibility, trace integrity, and fail-closed safety modes belong in the harness spec itself.',
      gives: ['Reversibility classification on every tool (FULL / PARTIAL / NONE)', 'SHA-256 trace input hashing + policy version fingerprint', '`require_approval` gate on the tool-call path', '`safety_mode: strict` — fail-closed, deny on no-match'],
      color: A.worker,
    },
    {
      num: 'AG', date: 'May 2026', source: 'Yohei Nakajima',
      title: 'ActiveGraph: Event-Sourced Agents',
      arxiv: '2605.21997',
      link: 'https://arxiv.org/abs/2605.21997',
      insight: 'Append-only event logs make agent runs reproducible and auditable. Content-addressed LLM caching turns expensive re-runs into instant cache hits — enabling replay, debugging, and future fork-and-diff without paying LLM costs.',
      gives: ['Content-addressed LLM response cache (`--no-cache` to opt out)', '`armature replay <run_id>` — stage-by-stage audit from TraceStore', 'Trace-triggered behaviors (`BehaviorRule`) with IHR feedback built-in', '`--auto-improve`: after each run, auto-applies spec improvements when IHR drops below 0.75'],
      color: A.judge,
    },
    {
      num: 'KYA', date: 'May 2026', source: 'Veldt Labs',
      title: 'KYA: Trust Layer for Autonomous Systems',
      arxiv: '2605.25376',
      link: 'https://arxiv.org/abs/2605.25376',
      insight: 'Governance must operate before execution, not only at runtime. A risk score computed from the agent\'s definition — its tools, governance mode, and safety rules — tells you how dangerous a workflow is before it runs. And safety rules must only tighten: an allow rule that contradicts a block rule is a misconfiguration, not a feature.',
      gives: ['Static spec risk score [0–100] surfaced by `armature validate` (LOW/MEDIUM/HIGH/CRITICAL)', 'Rogue signal counter — every tool block incremented, shown in run summary', 'Only-tighten rule validation — `CONFLICTING_SAFETY_RULES` when allow loosens a block'],
      color: A.orchestrator,
    },
  ];

  return (
    <section id="research" style={{
      backgroundColor: A.surface,
      borderTop: `1px solid ${A.border}`,
      borderBottom: `1px solid ${A.border}`,
      padding: '88px 32px',
    }}>
      <div style={{ maxWidth: 1060, margin: '0 auto' }}>
        <Reveal>
          <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
            Research Foundation
          </span>
          <h2 style={{
            fontFamily: SERIF, fontSize: 36, fontWeight: 400,
            color: A.textBright, lineHeight: 1.25, margin: '12px 0 12px',
          }}>
            Ten sources. One framework.
            <br /><span style={{ color: A.researcher, fontStyle: 'italic' }}>All implemented.</span>
          </h2>
          <p style={{
            fontFamily: SANS, fontSize: 16, color: A.textSecondary,
            lineHeight: 1.7, margin: '0 0 16px', maxWidth: 620,
          }}>
            Armature isn&apos;t invented from first principles — it&apos;s a synthesis of the
            best current academic thinking on agent harness design, published between
            February and May 2026, plus Microsoft&apos;s Agent Governance Toolkit,
            ActiveGraph&apos;s event-sourced execution model, and Veldt Labs&apos; KYA trust layer.
            Every source contributed concrete, implemented capabilities.
          </p>
          <p style={{
            fontFamily: SANS, fontSize: 15, color: A.textMuted,
            lineHeight: 1.7, margin: '0 0 48px', maxWidth: 620,
            borderLeft: `2px solid ${A.border}`, paddingLeft: 16,
          }}>
            <em>Mature</em> has two meanings here. The agents grow smarter every run —
            and the harness itself matures alongside the field, tracking the latest research
            as it ships.
          </p>
        </Reveal>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          {papers.map((p, i) => {
            const cardStyle = {
              padding: '24px 26px',
              backgroundColor: A.bg,
              border: `1px solid ${A.border}`,
              borderRadius: 10,
              borderTop: `2px solid ${p.color}40`,
              height: '100%',
              transition: 'border-color 0.15s',
            };
            const card = (
              <div style={cardStyle}>
                {/* Header row */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 10 }}>
                  <span style={{ fontFamily: MONO, fontSize: 10, fontWeight: 700, color: p.color, letterSpacing: '0.08em' }}>
                    {p.num} · {p.date}
                  </span>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    {p.arxiv && (
                      <span style={{ fontFamily: MONO, fontSize: 9, color: A.textMuted }}>
                        arXiv:{p.arxiv}
                      </span>
                    )}
                    {p.link && (
                      <span style={{ fontFamily: MONO, fontSize: 10, color: p.color, opacity: 0.7 }}>↗</span>
                    )}
                  </div>
                </div>

                <h3 style={{
                  fontFamily: SERIF, fontSize: 17, fontWeight: 500,
                  color: A.textBright, margin: '0 0 10px', lineHeight: 1.3,
                }}>{p.title}</h3>

                <div style={{ fontFamily: MONO, fontSize: 10, color: A.textMuted, marginBottom: 12 }}>{p.source}</div>

                <p style={{
                  fontFamily: SANS, fontSize: 13, color: A.textSecondary,
                  lineHeight: 1.65, margin: '0 0 16px',
                  borderLeft: `2px solid ${p.color}30`,
                  paddingLeft: 12,
                  fontStyle: 'italic',
                }}>{p.insight}</p>

                <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                  {p.gives.map((g, j) => (
                    <div key={j} style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                      <span style={{ color: p.color, fontSize: 9, marginTop: 4, flexShrink: 0 }}>▸</span>
                      <span style={{ fontFamily: MONO, fontSize: 11, color: A.textMuted, lineHeight: 1.4 }}>{g}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
            return (
              <Reveal key={i} delay={i * 0.06}>
                {p.link
                  ? <a href={p.link} target="_blank" rel="noopener noreferrer" style={{ display: 'block', textDecoration: 'none', height: '100%' }}>{card}</a>
                  : card
                }
              </Reveal>
            );
          })}
        </div>

        <Reveal delay={0.4}>
          <div style={{
            marginTop: 24, padding: '18px 24px',
            backgroundColor: 'rgba(74,222,128,0.04)',
            border: `1px solid rgba(74,222,128,0.12)`,
            borderRadius: 8, textAlign: 'center',
          }}>
            <p style={{ fontFamily: SERIF, fontSize: 15, fontStyle: 'italic', color: A.textSecondary, margin: 0, lineHeight: 1.6 }}>
              The core finding shared across all seven:{' '}
              <span style={{ color: A.text }}>the harness is more important than the model.</span>
              {' '}Armature ships the harness — production-grade, self-improving, and open source.
            </p>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

// ─── CODE EXAMPLE ─────────────────────────────────────────────────────────────
function CodeExample() {
  const yaml = `name: market-briefing
model_tiers:
  small: {provider: anthropic, model: claude-haiku-4-5-20251001}
  large: {provider: anthropic, model: claude-sonnet-4-6}

stages:
  - id: researcher
    role: researcher
    tier: small
    system: |
      Gather and summarize key signals on the given topic.
      Focus on recent developments, key players, and trends.

  - id: analyst
    role: worker
    tier: small
    depends_on: [researcher]
    system: |
      From the research, identify the top 3 opportunities.
      Quantify each with available evidence.

  - id: editor
    role: judge
    tier: large
    depends_on: [analyst]
    system: |
      Review the analysis. Score quality 0–10.
      Flag any gaps or unsupported claims.`;

  const terminal = [
    { sym: '$', color: A.textMuted, text: 'armature run market-briefing.yaml \\' },
    { sym: ' ', color: A.textMuted, text: '  --topic "AI in healthcare diagnostics"' },
    { sym: '✓', color: A.researcher, text: 'DAG validated (3 stages, no cycles)' },
    { sym: '◌', color: A.worker, text: 'researcher    running...' },
    { sym: '✓', color: A.researcher, text: 'researcher    done  (1.4s)' },
    { sym: '◌', color: A.worker, text: 'analyst       running...' },
    { sym: '✓', color: A.researcher, text: 'analyst       done  (2.2s)' },
    { sym: '◌', color: A.worker, text: 'editor        running...' },
    { sym: '✓', color: A.researcher, text: 'editor        done  (0.9s, score=8.7/10)' },
    { sym: '✓', color: A.researcher, text: 'Complete in 4.5s · IHR=0.91' },
    { sym: '→', color: A.textMuted, text: '.armature/traces/run-20260517.json' },
  ];

  return (
    <section style={{
      backgroundColor: A.surface,
      borderTop: `1px solid ${A.border}`,
      borderBottom: `1px solid ${A.border}`,
      padding: '88px 32px',
    }}>
      <div style={{ maxWidth: 1060, margin: '0 auto' }}>
        <Reveal>
          <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
            Quick Start
          </span>
          <h2 style={{
            fontFamily: SERIF, fontSize: 36, fontWeight: 400,
            color: A.textBright, lineHeight: 1.25, margin: '12px 0 12px',
          }}>
            From zero to running
            <br /><span style={{ color: A.worker, fontStyle: 'italic' }}>in minutes.</span>
          </h2>
          <p style={{
            fontFamily: SANS, fontSize: 16, color: A.textSecondary,
            lineHeight: 1.7, margin: '0 0 48px', maxWidth: 520,
          }}>
            Write a YAML spec, point Armature at it, and watch your maturity of agents get to work.
          </p>
        </Reveal>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
          {/* YAML */}
          <Reveal>
            <div style={{ borderRadius: 10, overflow: 'hidden', border: `1px solid ${A.border}` }}>
              <div style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
                backgroundColor: A.surfaceRaised, borderBottom: `1px solid ${A.border}`,
              }}>
                {['#3D3D3D','#3D3D3D','#3D3D3D'].map((c,i) => <span key={i} style={{ width: 9, height: 9, borderRadius: '50%', backgroundColor: c }} />)}
                <span style={{ fontFamily: MONO, fontSize: 10, color: A.textMuted, marginLeft: 6 }}>market-briefing.yaml</span>
              </div>
              <pre style={{
                backgroundColor: '#0a0e14', padding: '22px 20px',
                fontFamily: MONO, fontSize: 12, lineHeight: 1.7,
                color: '#cdd6f4', overflowX: 'auto', margin: 0,
              }}>{yaml}</pre>
            </div>
          </Reveal>

          {/* Terminal */}
          <Reveal delay={0.1}>
            <div style={{ borderRadius: 10, overflow: 'hidden', border: `1px solid ${A.border}` }}>
              <div style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
                backgroundColor: A.surfaceRaised, borderBottom: `1px solid ${A.border}`,
              }}>
                {['#3D3D3D','#3D3D3D','#3D3D3D'].map((c,i) => <span key={i} style={{ width: 9, height: 9, borderRadius: '50%', backgroundColor: c }} />)}
                <span style={{ fontFamily: MONO, fontSize: 10, color: A.textMuted, marginLeft: 6 }}>terminal</span>
              </div>
              <div style={{ backgroundColor: '#0a0e14', padding: '22px 20px', minHeight: 340 }}>
                {terminal.map((line, i) => (
                  <div key={i} style={{ display: 'flex', gap: 10, marginBottom: 6 }}>
                    <span style={{ fontFamily: MONO, fontSize: 12, color: line.color, flexShrink: 0, width: 12 }}>{line.sym}</span>
                    <span style={{ fontFamily: MONO, fontSize: 12, color: i < 2 ? A.textMuted : line.sym === '✓' ? A.text : line.sym === '◌' ? '#6e7681' : A.textMuted, lineHeight: 1.5 }}>{line.text}</span>
                  </div>
                ))}
              </div>
            </div>
          </Reveal>
        </div>

        <Reveal delay={0.2}>
          <div style={{ marginTop: 24, padding: '16px 20px', borderRadius: 8, backgroundColor: A.bg, border: `1px solid ${A.border}` }}>
            <span style={{ fontFamily: MONO, fontSize: 12, color: A.researcher }}>$ pip install armature</span>
            <span style={{ fontFamily: MONO, fontSize: 12, color: A.textMuted }}> &nbsp;· then set ANTHROPIC_API_KEY and run.</span>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

// ─── ECOSYSTEM ────────────────────────────────────────────────────────────────
function Ecosystem() {
  const projects = [
    {
      name: 'Argus',
      icon: '🔐',
      tagline: 'Security & Quality Scanner',
      desc: 'Combines 7 industry tools (gitleaks, semgrep, gosec, etc.) with LLM code review in a single command. Scans repositories in 3-8 minutes. A production reference implementation of Armature.',
      link: 'https://github.com/bryansparks/argus',
      color: '#EC4899',
    },
    {
      name: 'Research-Analyst',
      icon: '📊',
      tagline: 'Automated Research Briefings',
      desc: 'Deep-dive research powered by web search, source extraction, and synthesis — declared as a YAML workflow. Supports continuation for weekly incremental briefings and scheduled runs.',
      link: 'https://github.com/bryansparks/Research-Project',
      color: '#06B6D4',
    },
  ];

  return (
    <section style={{ padding: '88px 32px', maxWidth: 1060, margin: '0 auto' }}>
      <Reveal>
        <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
          Built with Armature
        </span>
        <h2 style={{
          fontFamily: SERIF, fontSize: 36, fontWeight: 400,
          color: A.textBright, lineHeight: 1.25, margin: '12px 0 12px',
        }}>
          Reference implementations.
          <br /><span style={{ color: A.researcher, fontStyle: 'italic' }}>Production-ready.</span>
        </h2>
        <p style={{
          fontFamily: SANS, fontSize: 16, color: A.textSecondary,
          lineHeight: 1.7, margin: '0 0 48px', maxWidth: 620,
        }}>
          Open-source projects that showcase what Armature can do — security scanning, automated research, and more.
        </p>
      </Reveal>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
        {projects.map((proj, i) => (
          <Reveal key={i} delay={i * 0.1}>
            <a href={proj.link} target="_blank" rel="noopener noreferrer" style={{
              padding: '32px', height: '100%',
              backgroundColor: A.surface, border: `1px solid ${A.border}`,
              borderRadius: 12, display: 'flex', flexDirection: 'column', gap: 16,
              textDecoration: 'none', cursor: 'pointer',
              transition: 'border-color 0.2s ease',
            }}
            onMouseEnter={(e) => { e.currentTarget.style.borderColor = proj.color; }}
            onMouseLeave={(e) => { e.currentTarget.style.borderColor = A.border; }}>
              <div style={{ fontSize: 40 }}>{proj.icon}</div>
              <div>
                <div style={{
                  fontFamily: MONO, fontSize: 11, fontWeight: 700,
                  color: proj.color, textTransform: 'uppercase',
                  letterSpacing: '0.05em', marginBottom: 4,
                }}>{proj.name}</div>
                <h3 style={{
                  fontFamily: SERIF, fontSize: 22, fontWeight: 400,
                  color: A.textBright, margin: '0 0 8px',
                }}>{proj.tagline}</h3>
              </div>
              <p style={{
                fontFamily: SANS, fontSize: 14, color: A.textSecondary,
                lineHeight: 1.7, margin: 0, flex: 1,
              }}>{proj.desc}</p>
              <div style={{
                fontFamily: MONO, fontSize: 12, color: proj.color,
                marginTop: 8, display: 'flex', alignItems: 'center', gap: 6,
              }}>
                View on GitHub →
              </div>
            </a>
          </Reveal>
        ))}
      </div>
    </section>
  );
}

// ─── OPEN SOURCE CTA ──────────────────────────────────────────────────────────
function OpenSourceCTA() {
  return (
    <section id="open-source" style={{ padding: '88px 32px', maxWidth: 1060, margin: '0 auto' }}>
      <Reveal>
        <div style={{ textAlign: 'center', maxWidth: 640, margin: '0 auto' }}>
          <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
            Open Source
          </span>
          <h2 style={{
            fontFamily: SERIF, fontSize: 40, fontWeight: 400,
            color: A.textBright, lineHeight: 1.25, margin: '20px 0 20px',
          }}>
            Built to be shared.
          </h2>
          <p style={{
            fontFamily: SANS, fontSize: 16, color: A.textSecondary,
            lineHeight: 1.8, margin: '0 0 40px',
          }}>
            Armature is free, MIT licensed, and built in the open. Fork it, extend it,
            build on it. Contributions welcome — especially new role types, tool
            integrations, and self-improvement strategies.
          </p>

          <div style={{ display: 'flex', gap: 14, justifyContent: 'center', marginBottom: 40 }}>
            <a
              href="https://github.com/bryansparks/armature"
              target="_blank"
              rel="noopener noreferrer"
              style={{
                fontFamily: SANS, fontSize: 15, fontWeight: 600,
                color: A.bg, backgroundColor: A.researcher,
                padding: '14px 36px', borderRadius: 8, textDecoration: 'none',
                boxShadow: '0 2px 16px rgba(74,222,128,0.25)',
              }}
            >View on GitHub →</a>
            <a href="https://github.com/bryansparks/armature/blob/main/USER-GUIDE.md" style={{
              fontFamily: SANS, fontSize: 15, fontWeight: 500,
              color: A.textSecondary,
              padding: '14px 28px', borderRadius: 8, textDecoration: 'none',
              border: `1px solid ${A.border}`,
            }}>Read the Docs</a>
          </div>

          {/* Stats row */}
          <div style={{ display: 'flex', gap: 0, justifyContent: 'center', borderTop: `1px solid ${A.border}`, paddingTop: 32 }}>
            {[
              { val: 'Python 3.11+', lbl: 'runtime' },
              { val: 'MIT', lbl: 'license' },
              { val: 'LiteLLM', lbl: 'provider layer' },
              { val: '1,286+', lbl: 'tests passing' },
            ].map((s, i) => (
              <div key={i} style={{ flex: 1, textAlign: 'center', padding: '0 16px', borderRight: i < 3 ? `1px solid ${A.border}` : 'none' }}>
                <div style={{ fontFamily: SERIF, fontSize: 22, color: A.researcher, marginBottom: 4 }}>{s.val}</div>
                <div style={{ fontFamily: MONO, fontSize: 10, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{s.lbl}</div>
              </div>
            ))}
          </div>
        </div>
      </Reveal>
    </section>
  );
}

// ─── V0.2.0 FEATURES ──────────────────────────────────────────────────────────
function V0_2_0_Features() {
  const features = [
    {
      title: 'mission: field',
      desc: 'Anchors all LLM stages to a stated goal. Prevents drift in long workflows — the system automatically validates that each stage output moves toward the mission.',
      icon: '🎯',
    },
    {
      title: 'continuation: block',
      desc: 'Cross-run context carry-forward. Start where you left off. Enables incremental research, weekly briefings, or ongoing analysis without resetting state.',
      icon: '↻',
    },
    {
      title: 'armature watch + triggers:',
      desc: 'Cron and webhook daemon. Schedule workflows (6am daily), react to events, or expose HTTP endpoints that kick off runs. Turn any workflow into a service.',
      icon: '⏰',
    },
    {
      title: 'response_stage: true',
      desc: 'SSE token streaming. Converts any workflow into a streaming API. Perfect for chatbots, real-time UIs, and interactive agents.',
      icon: '↙',
    },
    {
      title: 'armature replay <run_id>',
      desc: 'Replay any run with cached LLM responses. Debug via transcript, understand exactly why a result was produced, or rerun with different safety rules.',
      icon: '▶️',
    },
    {
      title: 'Static risk scoring',
      desc: 'armature validate now scores LOW/MEDIUM/HIGH/CRITICAL before any run. Catch dangerous specs early with Governance layers, tool bans, and safety drift detection.',
      icon: '⚠️',
    },
  ];

  return (
    <section style={{ padding: '88px 32px', maxWidth: 1060, margin: '0 auto' }}>
      <Reveal>
        <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
          What's New in v0.2.0
        </span>
        <h2 style={{
          fontFamily: SERIF, fontSize: 36, fontWeight: 400,
          color: A.textBright, lineHeight: 1.25, margin: '12px 0 12px',
        }}>
          Long-running workflows.
          <br /><span style={{ color: A.researcher, fontStyle: 'italic' }}>Production hardened.</span>
        </h2>
        <p style={{
          fontFamily: SANS, fontSize: 16, color: A.textSecondary,
          lineHeight: 1.7, margin: '0 0 48px', maxWidth: 620,
        }}>
          v0.2.0 adds mission anchoring, cross-run continuation, triggers, streaming responses, and governance — making Armature workflows production-grade for services, scheduled jobs, and interactive systems.
        </p>
      </Reveal>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
        {features.map((f, i) => (
          <Reveal key={i} delay={i * 0.05}>
            <div style={{
              padding: '28px', height: '100%',
              backgroundColor: A.surface, border: `1px solid ${A.border}`,
              borderRadius: 12, display: 'flex', gap: 16, flexDirection: 'column',
            }}>
              <div style={{
                fontSize: 32, lineHeight: 1,
              }}>{f.icon}</div>
              <div>
                <h3 style={{
                  fontFamily: MONO, fontSize: 13, fontWeight: 700,
                  color: A.researcher, textTransform: 'uppercase',
                  letterSpacing: '0.05em', margin: '0 0 8px',
                }}>{f.title}</h3>
                <p style={{
                  fontFamily: SANS, fontSize: 14, color: A.textSecondary,
                  lineHeight: 1.7, margin: 0,
                }}>{f.desc}</p>
              </div>
            </div>
          </Reveal>
        ))}
      </div>
    </section>
  );
}

// ─── ELFTECH ATTRIBUTION ──────────────────────────────────────────────────────
function ElfTechAttribution() {
  return (
    <section style={{
      padding: '40px 32px',
      backgroundColor: A.surface,
      borderTop: `1px solid ${A.border}`,
      textAlign: 'center',
    }}>
      <p style={{
        fontFamily: SANS, fontSize: 14, color: A.textSecondary,
        lineHeight: 1.6, margin: 0, maxWidth: 600, marginLeft: 'auto', marginRight: 'auto',
      }}>
        Armature is an open-source project from{' '}
        <a href="https://elftech.com" style={{ color: A.researcher, textDecoration: 'none', fontWeight: 600 }}>
          ElfTech
        </a>{' '}
        — we build autonomous AI systems for operational work. Explore our full platform at{' '}
        <a href="https://elftech.com" style={{ color: A.researcher, textDecoration: 'none', fontWeight: 600 }}>
          elftech.com
        </a>.
      </p>
    </section>
  );
}

// ─── PAGE ─────────────────────────────────────────────────────────────────────
export default function ArmatureLanding() {
  return (
    <div style={{ backgroundColor: A.bg }}>
      <ScrollNav />
      <Hero />
      <TheMaturity />
      <HowItWorks />
      <ThreeRoles />
      <SelfImprovement />
      <V0_2_0_Features />
      <ResearchFoundation />
      <CodeExample />
      <Ecosystem />
      <OpenSourceCTA />
      <ElfTechAttribution />
      <Footer />
    </div>
  );
}
