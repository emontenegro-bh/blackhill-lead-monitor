# Create Proposal

Takes customer info and job scope, then builds a complete proposal package: Aspire contact lookup/creation, material quantities, cost estimate, and brand-voice proposal description ready to paste into Aspire.

## Input

$ARGUMENTS

If no arguments provided, ask for:
1. Customer name and phone/email
2. Property address
3. What work is needed (scope description, measurements if known)

## Workflow

### Step 1: Aspire Contact Lookup

Search for the customer in Aspire CRM using the MCP tools:
- `mcp__aspire-crm__search_contacts` by email, phone, or name
- If found: note the ContactID and any existing properties/opportunities
- If NOT found: create the contact using `mcp__aspire-crm__create_contact` with:
  - ContactTypeID: 8 (Prospect)
  - OwnerContactID: 6 (Evelin)
  - Set FirstName, LastName, Email, MobilePhone from input
- Report the contact status to the user (found existing / created new)

### Step 2: Material Calculations

Load and follow the materials-calc skill at `.claude/skills/materials-calc/SKILL.md`.

Calculate quantities for every material mentioned in the scope:
- **Mulch**: 2" depth, black. Bags if <13, cubic yards if ≥13
- **Planting soil**: 8" depth, round up to nearest 0.5 cy
- **Steel black edging**: 4" height, 16-ft sections, count sections needed
- **Rock/flagstone**: Use coverage tables from the skill
- **Sod**: By pallet (450 sqft), add 7% waste
- **Plants**: Calculate quantity from bed dimensions ÷ mature spread

If measurements aren't provided, ask. Don't guess square footage.

### Step 3: Cost Estimate

Load and follow the estimate skill at `.claude/skills/estimate/SKILL.md`.

Build an internal cost breakdown (NOT included in the proposal description):
- Materials: quantities × supplier cost
- Labor: estimated crew-hours × billing rate
- Present as a table to the user
- This is for internal pricing only — NEVER include in the proposal description

### Step 4: Proposal Description

Load and follow the aspire-proposals skill at `.claude/skills/aspire-proposals/SKILL.md`.

Generate the ProposalDescription1 HTML following all brand voice rules:
- Wrap in the required `<div>` font wrapper
- Open with assessment language, not filler
- Use `<h3>` headers, `<p>` and `<ul>/<li>` only
- Format plants as: qty - size CommonName, with description sub-bullet
- Always say "steel black edging"
- Include planting soil quantities
- Common names only, no botanical names
- No pricing, no bold, no em dashes
- Include Fort Worth / North Texas context where relevant

### Step 5: Deliver

Present the complete package to the user:

1. **Contact Status**: Found/created, ContactID, link to Aspire
2. **Materials Summary**: Table of all materials with quantities
3. **Internal Estimate**: Labor + materials cost breakdown (for Evelin's eyes only)
4. **Proposal Description**: The full HTML ready to paste into Aspire's ProposalDescription1 field
5. **Suggested Opportunity Name**: "[Service Type] - [Property Address]"
6. **Suggested Division**: Based on detected service type

## Rules

- NEVER include costs, pricing, or dollar amounts in the proposal description
- ALWAYS verify the contact exists before generating the proposal
- If the scope is ambiguous, ask clarifying questions before proceeding
- Use the content-marketing skill for any ad-facing copy, but proposals go through aspire-proposals
- Budget information stays in conversation — never in CompanyCam notes or proposal text
