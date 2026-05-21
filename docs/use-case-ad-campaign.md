# Use Case: Social Ad Campaign Automation
## AI Reasoning Automation — Reference Implementation #1

**Platform:** Armature + Steward + Tessera  
**Client:** Dangerous Pretzel Co., Salt Lake City  
**Date:** 2026-05-14  
**Status:** Architecture complete — MVP scoped, not yet built  

---

## The Problem

A boutique food shop owner wants to run social media ad campaigns but doesn't
have time to manage creative production, platform publishing, or analytics.
Today this takes 4–6 hours per campaign and either doesn't happen or gets
outsourced at high cost.

**The target experience:** On Monday morning the owner says what he wants to
promote. By Tuesday, approved ads are live. By Thursday, he knows whether to
keep running them or try something new. He interacts exactly twice.

---

## Brand Context

**Dangerous Pretzel Co.** — hand-crafted specialty soft pretzels, Salt Lake City.

- **Voice:** Irreverent, bold, playful. "Ruin Dinner." "Zero regrets."
  "Invented by monks, perfected for punks."
- **Visual:** Dark, high-contrast, artisanal food photography
- **Products:** Savory (Salty, Spicy Bee, BBK), Sweet (Saint, For The Kids),
  Salty Bombs, dips, drinks
- **Markets:** Walk-in B2C + catering + wholesale/commercial placement
- **Location:** 352 W 600 S, SLC — regional targeting (25mi radius for B2C)
- **Media:** City Weekly, Salt Lake Magazine, Tribune, Axios

This brand context lives in Tessera as a corpus. Every agent that touches copy
or creative loads it first.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         STEWARD                              │
│         (control plane — work items, approvals, history)     │
│                                                              │
│  CampaignRequest → ConceptApproval → CampaignLaunch         │
│       → MonitorCycle → OptimizeDecision                      │
└──────────────┬──────────────────────────────────────────────┘
               │ dispatch (reasoning_task)
               ▼
┌─────────────────────────────────────────────────────────────┐
│                        ARMATURE                              │
│      (workflow execution — multi-agent YAML pipelines)       │
│                                                              │
│  intake.yaml → concept-gen.yaml → production.yaml           │
│       → publish.yaml → monitoring.yaml → optimize.yaml       │
└──────┬──────────┬──────────┬──────────────────┬─────────────┘
       │          │          │                  │
       ▼          ▼          ▼                  ▼
  TESSERA    Image Gen    Platform          Analytics
  (brand     (DALL-E 3,   Publishers        Collectors
  knowledge, Runway ML)   (Meta, TikTok,   (per-platform
  past                    YouTube,          APIs → unified
  campaigns)              LinkedIn,         metrics schema)
                          Snapchat)
```

The owner interacts at two Steward approval gates:
1. **Intake submission** — describe the campaign (web form or SMS)
2. **Concept approval** — pick one of three generated concepts

Everything between and after those two moments is autonomous.

---

## Phase 1: Campaign Intake

**Steward work item:** `CampaignRequest` (type: `reasoning_task`)  
**Armature spec:** `intake.yaml`  
**Owner action:** Fills out a simple form or sends a text message

### Inputs

```json
{
  "feature": "Spicy Bee pretzel",
  "goal": "foot_traffic",
  "platforms": "recommend",
  "cta_text": "Come in Friday–Saturday 11am–9pm",
  "tone_nudge": "keep it edgy"
}
```

### Stages

**`brand_loader`** (researcher)  
Pulls from Tessera: product descriptions, brand voice guidelines, past campaign
performance data, seasonal specials. Grounds all downstream agents.

**`brief_builder`** (judge)  
Synthesizes inputs + brand context into a structured `CampaignBrief`. Enforces
voice fidelity — rejects generic food marketing language. Outputs:

```json
{
  "feature": "Spicy Bee pretzel",
  "headline_direction": "lead with the heat, land the irreverence",
  "tone": "irreverent, bold — 'Ruin Dinner' energy",
  "audience": "young adults 21-35, SLC food enthusiasts",
  "platforms": null,
  "cta": "Come in Friday–Saturday 11am–9pm",
  "constraints": []
}
```

**`platform_recommender`** (orchestrator)  
If platforms = "recommend", selects based on goal + audience + past performance:

| Goal | Primary platforms |
|------|-------------------|
| foot_traffic | Instagram, TikTok, Facebook |
| catering | LinkedIn, Facebook |
| wholesale | LinkedIn |
| online_order | Instagram, Facebook |

Outputs a `TargetedCampaignBrief` with platforms confirmed.

---

## Phase 2: Concept Generation

**Steward work item:** `ConceptGeneration` (auto-triggered after intake)  
**Armature spec:** `concept-gen.yaml`

### Stages

**`copy_generator`** (worker, fan_out=3)  
Generates 3 copy variants, each with a distinct angle:
1. Irreverent/edgy — leans hardest into the brand voice
2. Sensory/indulgent — emphasizes taste, texture, experience
3. Social proof — leans on media recognition and cult following

Each variant includes: headline (≤10 words), body copy (50–150 chars), CTA,
hashtag set.

**`visual_director`** (researcher)  
Translates brief + copy into image generation prompts. Describes composition,
color treatment, text overlay placement, and platform-specific framing
(9:16 vertical for TikTok/Stories, 1:1 square for feed).

**`image_generator`** (script adapter, fan_out=3)  
Calls DALL-E 3 with each visual prompt. Returns 3 image assets. Upgrade path:
branded photo library adapter (real product shots) replaces AI-generated
images once the loop is proven.

**`video_scripter`** (worker — conditional on platform selection)  
If TikTok or YouTube is targeted, writes a 15–30 second script:
- Hook (0–3s): the scroll-stopper line
- Body (3–20s): product vibe or demo
- CTA (20–30s): close with action

**`concept_assembler`** (orchestrator)  
Pairs copy variants with best-fit visuals. Produces 3 static concept cards
plus optionally 1 video concept. Each card:

```json
{
  "concept_id": "A",
  "platform_fit": ["instagram", "tiktok"],
  "copy": {
    "headline": "Your dinner called. It's not coming home.",
    "body": "Spicy Bee: honey heat that ruins mild pretzels forever.",
    "cta": "Friday–Saturday 11am–9pm. 352 W 600 S, SLC."
  },
  "visual_url": "...",
  "visual_prompt": "Dark moody closeup of golden pretzel dripping honey, chili flakes scattered, dramatic side lighting...",
  "rationale": "Edgy angle, headline does the brand voice work, visual carries the indulgence"
}
```

**`brand_judge`** (judge)  
Evaluates all concepts against brand voice, platform policy, CTA clarity,
and visual–copy coherence. Rejects hard failures. Ranks the rest.

---

## Phase 3: Owner Review

**Steward gate** — `ConceptApproval` work item presented to owner.

Owner sees the 3 concept cards side-by-side. Options:
- **Pick one** → triggers Phase 4
- **Give feedback** → Steward creates a `ConceptRefinement` work item;
  Armature re-runs concept-gen with feedback injected as context
- **Start over** → new `CampaignRequest`

This is the second and final owner interaction before ads go live.

---

## Phase 4: Production

**Steward work item:** `CampaignProduction` (auto-triggered after approval)  
**Armature spec:** `production.yaml`

### Stages

**`format_planner`** (researcher)  
Looks up current platform format requirements:

| Platform | Required formats |
|----------|-----------------|
| Instagram Feed | 1:1 (1080×1080), 4:5 (1080×1350) |
| Instagram Stories/Reels | 9:16 (1080×1920) |
| TikTok | 9:16 (1080×1920), 15–30s video |
| Facebook Feed | 1.91:1 (1200×628), 1:1 |
| Facebook Stories | 9:16 (1080×1920) |
| LinkedIn | 1.91:1 (1200×628), 1:1 |
| Snapchat | 9:16 (1080×1920) |
| YouTube pre-roll | 16:9, 15–30s video |

**`asset_resizer`** (script adapter, fan_out=N platforms)  
Resize/crop the approved image to each required format using Pillow. Overlay
text where the visual design calls for it.

**`video_producer`** (script adapter — conditional)  
Calls Runway ML Gen-3 with the approved script + reference image. Returns a
video file. Skipped in MVP.

**`compliance_checker`** (judge)  
Final pass: copy length within platform limits, no prohibited terms, aspect
ratios verified, file sizes within platform upload limits.

**`package_assembler`** (orchestrator)  
Bundles all assets into named folders per platform:

```
campaign-spicy-bee-2026-05-14/
├── instagram-feed/    (1080x1080.jpg, 1080x1350.jpg)
├── instagram-stories/ (1080x1920.jpg)
├── tiktok/           (1080x1920.jpg, video.mp4)
├── facebook-feed/    (1200x628.jpg, 1080x1080.jpg)
└── linkedin/         (1200x628.jpg)
```

---

## Phase 5: Publishing

**Steward work item:** `CampaignLaunch`  
**Armature spec:** `publish.yaml`

Each publisher stage is a script adapter calling the respective platform API:

| Stage | API | Key credentials needed |
|-------|-----|----------------------|
| `meta_publisher` | Meta Marketing API v19 | Ad Account ID, Page ID, access token |
| `tiktok_publisher` | TikTok Ads API | Advertiser ID, access token |
| `linkedin_publisher` | LinkedIn Campaign Manager API | Organization ID, token |
| `snapchat_publisher` | Snap Ads API | Ad Account ID, token |
| `youtube_publisher` | Google Ads API | Customer ID, OAuth credentials |

Each publisher:
1. Uploads creative asset
2. Creates ad copy object
3. Creates ad set with targeting (location: SLC, radius: 25mi for B2C;
   broader for catering/wholesale)
4. Sets schedule (start: now, end: defined campaign end date)
5. Returns: `{ platform, ad_id, status, preview_url }`

**`launch_summary`** (orchestrator)  
Aggregates all publisher results. Notifies owner: "Your Spicy Bee campaign
is live on Instagram, TikTok, and Facebook. Preview links: ..."

---

## Phase 6: Monitoring & Optimization Loop

**Trigger:** Daily scheduled job, starting 24h after launch  
**Steward:** Recurring `MonitorCycle` work items (Day 1, Day 3, Day 7)  
**Armature spec:** `monitoring.yaml`

### Stages

**`metrics_collector`** (script adapter, fan_out=N platforms)  
Calls each platform's analytics API. Collects:
- Impressions, reach
- CTR (click-through rate)
- Engagement rate (likes, shares, comments)
- Link clicks / profile visits
- Cost per click (if paid placement)
- Video: completion rate, average watch time

**`metrics_normalizer`** (worker)  
Maps platform-specific field names to a unified schema:

```json
{
  "platform": "instagram",
  "date": "2026-05-15",
  "impressions": 4200,
  "reach": 3100,
  "ctr": 0.042,
  "engagement_rate": 0.078,
  "link_clicks": 176,
  "video_completion_rate": null
}
```

**`performance_judge`** (judge)  
Evaluates against configurable thresholds (sensible defaults for a local
food brand):
- CTR ≥ 2% → good
- Engagement rate ≥ 5% → good
- Link clicks ≥ 50/day → good

Returns: `{ verdict: "continue" | "underperforming" | "pause" }`

**`dashboard_updater`** (worker)  
Generates or updates a simple HTML dashboard:
- Sparklines per platform
- Top-performing creative callout
- Running cost vs. engagement summary
- Day 1 / Day 3 / Day 7 trend columns

**`decision_gate`** (orchestrator)  
- `continue` → no action, next cycle scheduled
- `underperforming` after Day 3 → creates a `CampaignOptimize` work item in Steward
- `pause` → halts ads via platform APIs, notifies owner

**`optimizer`** (researcher — triggered on underperformance)  
Analyzes what didn't work: worst platform, copy variant performance, best
engagement time window. Creates a new `CampaignRequest` pre-seeded with
these findings → restarts Phase 1 with institutional memory.

---

## Component Map: What Exists vs. What Needs Building

### Exists Today

| Component | Role in this system |
|-----------|-------------------|
| **Armature** | Workflow orchestration, fan_out, judge roles, retry logic |
| **Steward** | Work item lifecycle, approval gates, run history |
| **Tessera** | Brand knowledge corpus, past campaign storage |
| **Steward `reasoning_task` dispatch** | Routes work items to Armature (see STEWARD_ENHANCEMENT.md) |
| **Armature LLM nodes** | Copy generation, brief synthesis, recommendations, judging |

### Needs Building

| Component | Complexity | MVP priority |
|-----------|-----------|-------------|
| Tessera brand corpus — Dangerous Pretzel | Low (data entry) | P1 |
| DALL-E 3 image generation adapter | Low | P1 |
| Pillow image resize script adapter | Low | P1 |
| Meta Ads publisher (Facebook + Instagram) | Medium | P1 |
| Meta Analytics collector | Medium | P1 |
| Intake web form (simple HTML) | Low | P1 |
| Campaign HTML dashboard generator | Low | P2 |
| TikTok Ads publisher | Medium | P2 |
| TikTok Analytics collector | Medium | P2 |
| LinkedIn publisher | Medium | P2 |
| Runway ML video generation adapter | Medium-High | P3 |
| YouTube / Snapchat publishers | Medium | P3 |

---

## MVP Scope (Phase 1 Build)

Prove the loop works end-to-end with only P1 components:

1. Owner submits via a structured HTML form (no conversational chat yet)
2. Armature generates 3 copy variants + 3 DALL-E 3 image concepts
3. Owner picks one concept via Steward approval UI
4. System resizes assets to Instagram and Facebook formats
5. System publishes to Instagram Feed + Facebook Feed via Meta API
6. Next day: Meta Analytics pulled, CTR and reach reported in a simple
   HTML dashboard
7. Day 3: performance judge runs; if underperforming, a new campaign
   request is suggested to the owner

**No video, no TikTok, no LinkedIn in MVP.**

**What this proves:**
- Full end-to-end loop works
- Brand voice is preserved ("Ruin Dinner" energy survives the pipeline)
- Owner saves 4+ hours per campaign
- Feedback loop closes within 72 hours

---

## Armature Workflow Spec: intake.yaml

```yaml
name: campaign-intake
version: "1.0"
description: "Campaign brief intake for Dangerous Pretzel Co."

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  large:
    provider: anthropic
    model: claude-sonnet-4-6

contracts:
  inputs:
    - name: feature
      type: string
      description: "Product or feature to highlight"
    - name: goal
      type: string
      description: "foot_traffic | catering | wholesale | online_order"
    - name: platforms
      type: string
      description: "Comma-separated platforms, or 'recommend'"
    - name: cta_text
      type: string
      description: "Call to action text, or blank to auto-generate"
    - name: tone_nudge
      type: string
      description: "Optional tone direction, e.g. 'keep it edgy'"

stages:
  - id: brand_load
    role:
      name: brand-researcher
      type: researcher
      model_tier: small
    description: >
      Load Dangerous Pretzel brand context: product descriptions, voice
      guidelines, past campaign performance. Output a brand_context summary.

  - id: brief_builder
    role:
      name: brief-synthesizer
      type: judge
      model_tier: large
    depends_on: [brand_load]
    description: >
      Build a CampaignBrief from the inputs and brand context. Voice must
      be irreverent and bold — "Ruin Dinner" energy. Reject generic food
      marketing language. If tone_nudge is set, honor it.
    output_mode: guided_json
    output_schema:
      type: object
      required: [feature, headline_direction, tone, audience, cta, constraints]
      properties:
        feature: { type: string }
        headline_direction: { type: string }
        tone: { type: string }
        audience: { type: string }
        platforms: { type: ["array", "null"], items: { type: string } }
        cta: { type: string }
        constraints: { type: array, items: { type: string } }

  - id: platform_confirm
    role:
      name: platform-planner
      type: orchestrator
      model_tier: small
    depends_on: [brief_builder]
    description: >
      If platforms is null or 'recommend', select based on goal:
      foot_traffic → [instagram, tiktok, facebook]
      catering → [linkedin, facebook]
      wholesale → [linkedin]
      online_order → [instagram, facebook]
      Otherwise pass through the owner's choice.
    output_mode: guided_json
    output_schema:
      type: object
      properties:
        platforms: { type: array, items: { type: string } }
        rationale: { type: string }
```

---

## Open Questions (Parking Lot)

1. **Ad account setup** — Does the shop have a Meta Business Manager
   account? TikTok Ads account? These are prerequisites for the publisher
   stages; they require human setup, not automation.

2. **Conversational intake** — The MVP uses a web form. The right end-state
   is an SMS intake: the owner texts "Spicy Bee weekend promo, keep it edgy"
   on Monday morning and the chain runs. Twilio adapter needed.

3. **Photo assets** — DALL-E 3 generates plausible pretzel imagery but not
   actual product photos. Once the loop is proven, swap in a photo library
   adapter (a folder of approved product shots on file) and use DALL-E only
   for backgrounds and lifestyle elements.

4. **Tessera corpus seeding** — Someone needs to load the actual product
   descriptions, seasonal calendar, and past campaign results into Tessera.
   This is data entry, not engineering, but it's critical for brand-voice
   fidelity.

5. **Multi-tenant path** — This system is built for one shop. Making it
   multi-tenant (other food businesses, other verticals) is the path to a
   product. Tessera's brand corpus + Steward's product enrollment model
   already support this — each client gets a product record and a brand
   corpus. Armature workflow specs are reusable without modification.

---

## The Bigger Picture

This use case is the first concrete instance of the **Reasoning Automation**
platform vision: Armature + Steward + Tessera working as a coordinated team
to own a business process end-to-end.

The same architecture, with different Armature specs and Tessera corpora:

| Use case | What changes |
|----------|-------------|
| **Social ad campaigns** (this doc) | Creative + platform adapters |
| **Contract risk review** | Legal analysis workflow + document corpus |
| **Vendor assessment** | Research + scoring workflow |
| **Compliance documentation** | Extraction + report generation workflow |
| **Software triage** (Steward native) | Anvil code execution path |

The platform layer — Armature's orchestration, Steward's approval lifecycle,
Tessera's knowledge grounding — is shared across all of them. The
domain-specific work lives in YAML specs and adapter scripts.
