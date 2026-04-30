# Instructions for Claude

## Core Philosophy

- **Execute, don't consult** - If you can do it, do it. Only report what needs human action.
- **Ask when uncertain** - Don't guess or assume.
- **Solve root causes** - Not symptoms (use Kaizen system for debugging).
- **Think twice, ship once** - Specs before code.
- **Verify before reporting** - Never report something as broken, missing, or wrong without checking the live source first. Use the browser (Playwright) for website checks, not cached/stale data. If an agent returns a finding, verify it independently before presenting it to the user. Do not relay stale audit data as current fact.
- **Verify before recommending** - Before making any recommendation (ad copy, extensions, assets, settings, content), first: (1) check whether it has already been implemented (query campaign-level AND account-level state, not just one), (2) verify it aligns with the brand voice and guidelines in `data/company/brand-profile.md` and the content-marketing skill, and (3) check claude-mem and skill files for past decisions that may have already addressed it. Never recommend something that contradicts documented brand voice rules or duplicates existing work.
- **Verify data before presenting** - Never present API query results, audit findings, or analytics as fact without cross-verification. Specifically: (1) cross-check API results against the live interface (Playwright) when data will drive decisions, (2) re-read your own data before summarizing — do not contradict your own results (e.g., listing converted terms as "waste"), (3) if a query returns unexpected results (e.g., "zero assets found" when past records show they existed), flag the discrepancy and investigate before reporting it as the current state, (4) when presenting financial data or performance metrics, double-check the numbers before sharing. Inaccurate data erodes trust and wastes time.

---

## System Architecture

```
CLAUDE.md       ← Universal principles (this file - rarely modified)
   ↓
Commands        ← Workflow orchestration, load from /data/
   ↓
Agents          ← Roles with scope (131 agents in flat structure)
   ↓
Skills          ← Deep how-to, procedures, templates (71 skills)
   ↔
Handoffs        ← Inter-skill context passing (18 docs, 9 bidirectional pairs)
   ↓
Scripts         ← Deterministic execution (zero AI tokens)
   ↓
States          ← Persistent state for cross-session continuity
```

**Downward Pressure**: Push everything as LOW as possible.

**Progressive Disclosure**: Skills use 3-tier loading: SKILL.md (always) → references/ (on-demand) → examples/ (when relevant).

**State Persistence**: Agents can maintain state across sessions via `.claude/states/` JSON files.

---

## Commands (53 Total)

### Spec-Driven Development

| Command | Purpose |
|---------|---------|
| `/create-steering-docs` | Create project context (product.md, tech.md, structure.md, roadmap.md) |
| `/specify [feature]` | Create PRD → SDD → PLAN with approval gates |
| `/implement [ID]` | Execute plan with TDD, phase-based approval |
| `/review [ID]` | Post-implementation review: quality, ADRs, reflexion, coverage, memory |
| `/validate [ID]` | Validate specs using 3 Cs (Completeness, Consistency, Correctness) |
| `/new [workflow]` | Build new commands/agents/skills with 10X framework |

### Quality & Testing

| Command | Purpose |
|---------|---------|
| `/test` | Run comprehensive test suite |
| `/coverage` | Identify untested paths, prioritize by business impact |
| `/quality` | Enforce gates (≥80% coverage, ≤10 complexity, 0 critical vulns) |
| `/review-code` | Multi-stage review (6 stages: automated → security → final) |
| `/refactor` | Refactor preserving behavior with TDD |

### Analysis & Debugging

| Command | Purpose |
|---------|---------|
| `/debug [issue]` | Four-phase systematic debugging |
| `/analyze` | Codebase analysis and tech debt assessment |
| `/critique` | Multi-agent feedback (josh-thinker → structurer → writer) |
| `/kaizen-why` | Five Whys root cause analysis |
| `/kaizen-fishbone` | Ishikawa diagram (6 categories) |
| `/kaizen-a3` | Toyota A3 problem-solving |
| `/kaizen-pdca` | Plan-Do-Check-Act cycle |
| `/kaizen-root-cause` | Systematic root cause trace |

### Multi-Agent Swarm

| Command | Purpose |
|---------|---------|
| `/swarm-analyze [topic]` | 3-5 agents research in parallel, then synthesize |
| `/swarm-debug [issue]` | Parallel debugging (Code Flow, Pattern Matcher, Error Analyzer, Root Cause) |
| `/swarm-implement [feature]` | Wave-based coordinated implementation |
| `/chain [commands]` | Chain commands with context passing |

### SEO/GEO Growth

| Command | Purpose |
|---------|---------|
| `/setup-seo` | Initialize .seo/ infrastructure and intake form |
| `/seo-audit` | 5-agent parallel audit (technical, content, competitor, backlink, LLM visibility) |
| `/seo-grow` | Full 4-phase system: Audit → Plan → Execute → Monitor |

### Business & Finance

| Command | Purpose |
|---------|---------|
| `/analyze-pl` | P&L analysis with profit lever identification |
| `/forecast-cash` | Cash flow projection from P&L |
| `/analyze-offer` | Hormozi Value Equation and Grand Slam framework |
| `/market` | TAM/SAM/SOM, competitive landscape, positioning |
| `/ads-analysis` | Google Ads optimization (Quality Score, CTR, ROAS) |
| `/lead-system` | Lead gen: landing pages, magnets, sequences, nurture |

### Content & Writing

| Command | Purpose |
|---------|---------|
| `/write [type] [topic]` | Generate Josh-style tweets, posts, or threads (type: tweet/post/thread) |
| `/blog [topic]` | Blog post with josh-thinker → structurer → writer pipeline |
| `/use-case [concept]` | 7-section AI use case article (2-3k words with code) |
| `/newsletter [topic]` | Newsletter using copywriter frameworks |
| `/social [transcript]` | Transform transcript into 8-12 tweet X thread |
| `/show [transcript]` | Extract mental models and thinking frameworks |
| `/refine [content]` | Make 10% clearer, 20% more resonant |
| `/tutorial [topic]` | Jupyter notebook tutorial with progressive complexity |

### Admin & Operations

| Command | Purpose |
|---------|---------|
| `/process-receipts` | PDF extraction → GL categorization → Excel report |
| `/save` | Save session overview to Claude Memory |
| `/remember [#tag]` | Search Claude Memory for context |
| `/memorize` | Store learnings in Claude Memory (semantic) |
| `/close` | Clean up session, archive notes |
| `/cleanup` | Remove temp files, build artifacts |
| `/meta` | Project metadata, steering status, spec inventory |

### Reflexion & Learning

| Command | Purpose |
|---------|---------|
| `/reflect` | Self-critique with complexity-based triage (Quick/Standard/Deep) |
| `/critique` | Multi-agent feedback loop |
| `/memorize` | Promote learnings to global memory |

---

## Key Workflows

### Spec-Driven Development
```
/create-steering-docs → /specify [feature] → /implement [ID] → /review [ID] → /memorize
```
- Steering docs provide stable project context
- Feature specs reference steering (don't duplicate)
- Traceability: PRD requirements → SDD components → PLAN tasks
- Full workflow: `.claude/docs/workflows/spec-driven-development.md`

### SEO/GEO Growth
```
/setup-seo → /seo-audit → /seo-grow
```
- 5-agent parallel audit
- 6-wave execution (technical → content → links → schema → outreach)
- All content through Josh agent pipeline for quality
- Pending folders for human review before publish

### Multi-Agent Analysis
```
/swarm-analyze [topic]
```
- Spawns 3-5 specialized agents
- Each analyzes from different perspective
- Synthesizes agreements, conflicts, combined insights

### Root Cause Debugging
```
/debug [issue] or /kaizen-why or /kaizen-fishbone
```
- Toyota Production System methodology
- Four-phase: Investigate → Pattern Analysis → Hypothesis → Fix
- Three strikes rule: after 3 failed fixes, question architecture

---

## Two-Tier Memory System

### Local (Spec-Specific)
- Learnings captured in `.claude/specs/[ID]/README.md`
- Feature-specific insights, gotchas, spec-reality mismatches
- Updated at phase boundaries during implementation

### Global (Claude Memory)
**Powered by thedotmack/claude-mem**
- Automatic observation capture via PostToolUse hook
- Session tracking and timeline
- Progressive disclosure search (10x token efficiency)
- Web UI: http://localhost:37777
- Categories: `domain`, `pattern`, `anti-pattern`, `decision`, `convention`, `rule`
- Searchable across all sessions
- Rich metadata with observation links

### Usage

```bash
# Search memories (progressive)
/remember [query]

# Store session learnings
/memorize

# Check system status
~/.claude/scripts/claude-mem-status.sh

# Browse web UI
open http://localhost:37777

# CLI Memory Viewer (view migrated historical memories)
~/.claude/memory/scripts/view-memories.py list
~/.claude/memory/scripts/view-memories.py search "topic"
~/.claude/memory/scripts/view-memories.py stats
~/.claude/memory/scripts/view-memories.py view <id>

# Full guide: ~/.claude/docs/memory-viewer-cheatsheet.md
```

---

## Automation Hooks

| Hook | Trigger | Purpose |
|------|---------|---------|
| `smart-install.js` | SessionStart | Auto-install claude-mem dependencies |
| `worker-service.cjs` | SessionStart | Start memory worker on port 37777 |
| `context_monitor.py` | UserPromptSubmit | Alerts at 85% (yellow) and 95% (red) context |
| `agent-evaluator.sh` | UserPromptSubmit | Presents agent catalog for evaluation |
| `skill-evaluator.sh` | SubagentStart | Presents skills for agent to evaluate and use |
| `auto_memorize.py` | PostToolUse | Captures every tool execution as observation |
| `auto_memorize.py` | Stop/SubagentStop | Queues session learnings for review |

---

## Google Accounts

Two Google accounts configured with separate MCP servers:

### Schultz Workspace (`hello@joshuaschultz.com`)

| Service | MCP Server | Auth |
|---------|------------|------|
| Gmail | `gmail-schultz` | OAuth2 |
| Calendar | `google-calendar-schultz` | OAuth2 |
| Search Console | `gsc-schultz` | OAuth2 |

### Personal (`joshuamschultz@gmail.com`)

| Service | MCP Server | Auth |
|---------|------------|------|
| Gmail | `gmail` | Service Account |
| Calendar | `google-calendar` | Service Account |
| Analytics | `google-analytics` | Service Account |

**Credentials Location:** `~/.config/gcloud/`

---

## MCP Tools

### Decision Matrix

| Need | Use | NOT |
|------|-----|-----|
| Library/framework docs | **Ref** | Tavily, Firecrawl |
| API documentation | **Ref** | WebSearch |
| Read specific URL content | **Ref** (`ref_read_url`) | Tavily extract |
| General web search | Tavily or Firecrawl | - |
| Scrape/crawl websites | Firecrawl | Tavily |
| Browser automation/testing | Playwright | - |
| Semantic memory search | claude-mem | Grep |
| Schultz email/calendar/GSC | `*-schultz` servers | - |
| Personal email/calendar/GA | `gmail`, `google-calendar`, `google-analytics` | - |
| Text-to-speech / audio | elevenlabs | - |
| Google Ads data/management | `google-ads` MCP | Manual exports |

### Ref (Documentation First)

**PREFERRED for all documentation lookups.** Use before generic web search.

```
# Search docs for a library/framework
mcp__Ref__ref_search_documentation(query="react useEffect cleanup")

# Read specific URL from search results
mcp__Ref__ref_read_url(url="https://react.dev/reference/...")
```

**Use for:** React, Next.js, Python, Node.js, any library/framework docs, API references, GitHub repos.

### Firecrawl (Web Scraping)

Best scraper available. Use for content extraction and site crawling.

```
# Single page scrape
mcp__firecrawl__firecrawl_scrape(url="...", formats=["markdown"])

# Search web (returns URLs, then scrape relevant ones)
mcp__firecrawl__firecrawl_search(query="...", limit=5)

# Map site structure
mcp__firecrawl__firecrawl_map(url="...")
```

### Tavily (Web Search)

General web search with optional content extraction.

```
mcp__tavily__tavily_search(query="...", max_results=5)
mcp__tavily__tavily_extract(urls=["..."])
```

### Playwright (Browser Automation)

For testing, screenshots, and interactive browser tasks.

```
mcp__playwright__browser_navigate(url="...")
mcp__playwright__browser_snapshot()
mcp__playwright__browser_click(element="...", ref="...")
```

### claude-mem (Semantic Memory)

Vector database for cross-session memory. Used by `/memorize` and `/remember`.

```
# Query memories
mcp__claude-mem__chroma_query_documents(
  collection_name="claude-memory",
  query_texts=["project-name topic"]
)
```

### gmail

Read, send, and manage Gmail messages.

```
# List messages
mcp__gmail__list_messages(maxResults=10)

# Send email
mcp__gmail__send_email(to="...", subject="...", body="...")
```

### google-calendar

Manage Google Calendar events.

```
# List upcoming events
mcp__google-calendar__list_events(maxResults=10)

# Create event
mcp__google-calendar__create_event(summary="...", start="...", end="...")
```

### elevenlabs

Text-to-speech and audio generation. Official ElevenLabs MCP server.

```
# List available voices
mcp__elevenlabs__list_voices()

# Generate speech from text
mcp__elevenlabs__text_to_speech(text="...", voice_id="...")

# Get voice info
mcp__elevenlabs__get_voice(voice_id="...")
```

**Use for:** TTS generation, voice listing, audio creation.

---

## Finding Things

| Resource | Location |
|----------|----------|
| Commands (53) | `.claude/commands/` |
| Agents (131) | `.claude/agents/CLAUDE.md` |
| Skills (71) | `.claude/skills/CLAUDE.md` |
| Handoffs (18) | `~/.claude/handoffs/` |
| States | `.claude/states/` |
| Rules | `.claude/rules/` (auto-loaded) |
| Workflows | `.claude/docs/workflows/` |
| Templates | Inside relevant skills |
| Steering (per-project) | `.claude/steering/` |

---

## Agent Categories

| Category | Count | Examples |
|----------|-------|----------|
| AI/LLM | 7 | llm-architect, nlp-engineer, prompt-engineer |
| Backend | 6 | api-designer, graphql-architect, fullstack |
| Business | 20+ | financial-analyst, compliance-auditor, hr-agent |
| Frontend | 6 | react-specialist, nextjs-developer, vue-expert |
| Quality | 12 | test-strategy-planner, coverage-analyzer, debugger |
| Research | 7 | codebase-analyzer, codebase-locator, web-search-researcher |
| Security | 4 | security-engineer, penetration-tester |
| Writing | 9 | legendary-copywriter, josh-writer, technical-writer |

Full catalog: `.claude/agents/CLAUDE.md`

---

## Skill Categories

| Category | Skills | Examples |
|----------|--------|----------|
| Architecture | 5 | adr-generator, pattern-enforcer, tech-debt-tracker |
| Business/Finance | 4 | cash-flow-forecasting, profit-lever-identification |
| HR | 2 | job-description (external), job-role (internal) |
| Marketing | 8 | google-ads-analysis, hormozi-offer, seo-growth |
| Spec-Driven | 4 | agent-delegation, specification-compliance |
| Testing | 14 | unit-test-writer, e2e-test-writer, coverage-gap-finder |
| Workflow | 12 | requirements-engineer, design-researcher, task-planner |

Full catalog: `.claude/skills/CLAUDE.md`

---

## The /data/ Pattern

Company/project-specific context:

```
/data/
├── company/     # About, voice, standards
├── hr/          # Job templates, interview guides
├── marketing/   # Personas, campaigns, style
├── sales/       # Pricing, competitors
└── templates/   # Shared templates
```

Commands load from `/data/` and pass to agents.

---

## State Files Pattern

Persistent state for agents that need cross-session continuity.

```
/.claude/states/
├── README.md      # Pattern documentation
├── health.json    # Health planning state
└── {domain}.json  # Future state files
```

### Purpose

State files allow agents to:
- **Track progress**: Week numbers, phases, cycles
- **Leave notes**: Ideas and reminders for future sessions
- **Maintain history**: Archive of past actions and learnings
- **Handle transitions**: Phase changes, deloads, milestones

### State File Structure

```json
{
  "current": { /* Current position/context */ },
  "next_planning": { /* AI notes for next session */ },
  "future": { /* Longer-term planning */ },
  "history": { /* Archive of past states */ },
  "meta": { "last_updated": "YYYY-MM-DD" }
}
```

### Usage Pattern

Agents with state files should:
1. **Load state FIRST** - Before any other operations
2. **Announce position** - "Week X of Y: Phase - Focus: [...]"
3. **Use AI notes** - Incorporate ideas from previous session
4. **Update state LAST** - After completing work, increment and leave notes

### Current State Files

| File | Agent | Purpose |
|------|-------|---------|
| `health.json` | health-week-planner | Weekly health programming (week, phase, focus, AI notes) |

---

## Skill Handoff System

Inter-skill communication documents at `~/.claude/handoffs/` that pass context between skills and reduce redundant API calls.

### How It Works

- 18 documents across 9 bidirectional pairs connecting 10 skills
- Each document has a **Current** section (overwritten each time) and **History** table (append-only)
- **All non-blocking**: if a handoff is missing, the skill notifies the user and proceeds
- **Staleness**: handoffs older than 14 days trigger a warning
- Status flow: `awaiting-handoff` → `ready-for-{target}` → `consumed`

### Connected Skills

| Pair | Documents |
|------|-----------|
| Content Marketing ↔ Google Ads | `content-to-ads.md` / `ads-to-content.md` |
| Local SEO ↔ Content Marketing | `seo-to-content.md` / `content-to-seo.md` |
| Local SEO ↔ GBP Management | `seo-to-gbp.md` / `gbp-to-seo.md` |
| Estimate ↔ Aspire Proposals | `estimate-to-proposals.md` / `proposals-to-estimate.md` |
| Marketing Analysis ↔ Google Ads | `marketing-to-ads.md` / `ads-to-marketing.md` |
| Azuga Fleet ↔ Route Scheduler | `fleet-to-routes.md` / `routes-to-fleet.md` |
| Lead Match ↔ Google Ads | `leads-to-ads.md` / `ads-to-leads.md` |
| GBP Management ↔ Content Marketing | `gbp-to-content.md` / `content-to-gbp.md` |
| Google Ads ↔ Local SEO | `ads-to-seo.md` / `seo-to-ads.md` |

### For Skills

- **Before starting work**: check your handoff read documents (listed in your SKILL.md Handoffs section)
- **After completing work**: update your handoff write documents
- If a handoff is missing or stale, notify the user and proceed

### Commands

- `/handoff-status` -- dashboard showing all 18 handoffs with status, age, and alerts
- Full documentation: `~/.claude/handoffs/README.md`

---

## Execution Rules

### Always
- Use TodoWrite to plan and track tasks
- Verify before claiming success (fresh output)
- Run diagnostics before AND after changes

### For Implementation
- TDD: Test → Implement → Validate
- Read files before changing them
- Approval at phase boundaries, not per-task

### For Analysis
- Analyze first
- Execute all actionable findings immediately
- Report only genuine blockers
- Never create phased timelines for humans

### For Debugging
- Use Kaizen system for systematic root cause analysis
- Three strikes rule: after 3 failed attempts, question architecture
- Never bundle multiple changes together

---

## When in Doubt

1. Check commands: `ls .claude/commands/`
2. Check skills: `ls .claude/skills/`
3. Check agents: `cat .claude/agents/CLAUDE.md`
4. Check workflows: `ls .claude/docs/workflows/`
5. Ask the user

---

## API Reference Notes
- Aspire OData API: Phone fields are `MobilePhone`, `HomePhone`, `OfficePhone` (NOT `Phone` or `PhoneCell`). Opportunities endpoint returns 403 — use Property-level queries instead. API URLs do NOT include `/api/` prefix.
- CompanyCam API: `created_after` filter behaves like `updated_after` — use client-side filtering for accurate date ranges.
- WhatConverts API: Updates may return success but not persist — always verify writes with a follow-up read.

## Model IDs
- Use `claude-sonnet-4-20250514` for Anthropic API calls. Do NOT guess model IDs — this is the verified working ID.

## Proposal Monitor
- The proposal monitor must use client-side date filtering (not API `created_after`/`updated_after`) to avoid reprocessing old projects.
- Email reply monitoring must filter out historical messages — only process messages newer than the workflow start time.
- Polling interval is 3 minutes (not 10).

## Reporting
- When generating Excel/spreadsheet reports, always apply strong visible color coding (not subtle fills). Verify formatting is visible by describing the applied styles back to the user.
- WhatConverts has zero revenue/value data — ROI must be calculated by cross-referencing with Aspire CRM won opportunities.

## General Workflow Rules
- For email-sending tasks, confirm send completion and log the result.
- All new GitHub Actions workflows MUST include a failure notification job. Use the reusable workflow `.github/workflows/notify-failure.yml` in the `blackhill-lead-monitor` repo, or add an inline email notification step for workflows in other repos. No workflow should fail silently.

## Plan Before External API Calls
- Before making calls to external APIs (Aspire, WhatConverts, CompanyCam, Google Ads, HubSpot), briefly state: the endpoint/URL, the field names you'll use, and the expected response format.
- If the API is documented in this file's "API Reference Notes" section, use the documented field names — do not guess alternatives.
- For multi-step API workflows (e.g., authenticate → query → update), outline all steps before executing the first one.

## Verification Rules
- **API writes**: Always verify by reading back the data after updates. WhatConverts is especially prone to silent failures.
- **API debugging**: Check URL format, endpoint permissions, and field names FIRST before retrying the same call.
- **GitHub Actions deployments**: After deploying a new or modified workflow, trigger a test run and verify output before considering done. Check that enum/string formatting matches local behavior.
- **Data before presenting**: Cross-check API results against the live interface when data will drive decisions. Re-read your own data before summarizing — do not contradict your own results.
- **Pre-deployment checklist**: Before pushing any GitHub Actions workflow, validate YAML syntax (`python3 -c "import yaml; yaml.safe_load(open('file.yml'))"`) and confirm all secrets/env vars are configured in the repo.

## Working with Documents & Images
- **Confirm measurements**: When extracting measurements from images, PDFs, or plan documents, state the extracted values explicitly and ask the user to confirm before using them in calculations.
- **Never assume units**: If unclear whether a value is tons vs yards, sq ft vs sq yards, linear ft vs sq ft, etc., ask. Do not default or guess.
- **PDF limitations**: If a PDF contains encoded binary content or complex layouts that prevent text extraction, say so immediately rather than making multiple failing attempts. Suggest the user paste the relevant text or numbers.
- **Image limitations**: If an image is too small, blurry, or complex to read confidently, say so and ask the user to describe the relevant details rather than guessing.
- **Material calculations**: Always show the formula and intermediate steps so the user can spot errors. Cross-check quantities against common-sense ranges (e.g., a 500 sq ft bed should not need 50 yards of mulch).

---

**Version**: 5.4 (2026-04-26)
**Commands**: 55 | **Agents**: 131 | **Skills**: 72
**Philosophy**: Router, not encyclopedia.
