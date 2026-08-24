---
name: claire-idmc-agent
description: >
  Use this skill for ALL queries involving Informatica IDMC, CLAIRE, CDGC, Snowflake tables,
  data catalog, data discovery, data lineage, data quality, data preview, data profiling,
  data cleansing, data integration, MDM, golden records, Customer 360, Product 360,
  business glossary, metadata, or any Informatica product questions.
  Also triggers when preparing for meetings or responding to emails involving customer data,
  Snowflake tables, or data catalog topics.
  Trigger keywords: show tables, find asset, data preview, data quality, data lineage,
  data profiling, create mapping, cleanse data, generate rules, MDM records,
  "prepare me for the meeting", "prepare me for the day" CLAIRE, IDMC, CDGC, Informatica.
---

# CLAIRE IDMC Agent

Invoke `claire-orchestrator-ai-agent` immediately with the user's query. Do not ask for
clarification first — CLAIRE will ask if needed.

---

## Step 0 — Start of session (FIRST action in every new chat)

Call `new_conversation` with no arguments **before anything else** in this chat.
This creates a fresh Informatica conversation ID so CLAIRE has clean context.
Call it only once per chat — do not call it again mid-conversation.

```
Tool: new_conversation
Args: {}
```

> **Why?** Claude Desktop keeps the MCP server running across chats — there is no
> automatic reset when you click "New Chat". Calling `new_conversation` explicitly
> ensures each chat gets its own isolated CLAIRE context with no bleed-over from
> previous conversations.

---

## Step 1 — Choose the right skill

### OrchestratorSkill
All CDGC catalog queries: asset discovery, data preview, data quality analysis, data
profiling, data cleansing, rule generation, rule recommendation, data integration/mapping,
lineage, product documentation, and general Informatica questions.

**Discovery & exploration:** find assets, show tables, search catalog, show columns,
show lineage, prepare for meeting about data, "where is the data for X"

**Data preview & analysis:** "show me 50 rows from sales_orders", "filter orders to last
month", "top 10 products by returns", "visualize customers by state",
"calculate average sales per region"

**Data quality:** "show data quality rules for CUSTOMERS", "what is the DQ score for
ORDERS?", "why is data quality low for this asset?", "what is the completeness of X?",
"show worst performing dimensions", "recommend rules to improve accuracy of Y",
"suggest cleaning rules", "cleanse CUSTOMERS table to remove duplicates",
"generate DQ rules from uploaded file", "assess dataset and generate rules"

**Data profiling:** "is CUSTOMERS profiled?", "show profile of ORDERS",
"what is the null count for column X?", "show outliers in PRODUCTS",
"what is the completion rate of field Y?"

**Data integration:** "create a mapping for CUSTOMERS table",
"create a mapping for customers in North America", "create a mapping from the above result"

**Product documentation:** "how do I configure data sync in Informatica Cloud?",
"what are the new features in IDMC version X?", "explain the data lineage feature",
"troubleshoot connection timeout errors", "best practices for data integration performance",
"what data sources are supported?", "how are customer prompts stored?"

```
Tool: claire-orchestrator-ai-agent → OrchestratorSkill
Args: { "prompt": "<verbatim user query>" }
```

### MdmOrchestratorSkill
MDM-backed assets and entities only: golden records, survivorship, Customer 360,
Product 360, MDM entity records and relationships.

**Sample queries:** "show me 50 records from Customer entity",
"find customers in CA with status=active", "visualize customers by state",
"count accounts by risk_rating", "how is Alex connected to Maria?",
"is A married to B?", "show relationships between entity X and record Y",
"show golden records for Acme Corp", "Customer 360 view for account #1001"

```
Tool: claire-orchestrator-ai-agent → MdmOrchestratorSkill
Args: { "prompt": "<verbatim user query>" }
```

---

## Step 2 — Poll for progress with `working`

Immediately after invoking either skill, call `working` with no arguments.
Keep calling it in a tight loop until you receive `ANSWER:` or `done`.

**After EVERY `working` call, check the FIRST LINE of the response and act immediately:**

| First line starts with | Action |
|---|---|
| Plain text (no prefix) | **Output the text as your own message** to the user, exactly as received, line by line. Then call `working` again |
| `PROBE:` | **Ask** the user the question (everything after "PROBE: "), then **stop** polling |
| `ANSWER:` | **Render** the answer (see Rendering section below), then **stop** polling |
| `Still working…` | Call `working` again silently — do not show this to the user |
| `done` | **Stop** polling — nothing further to display |
| `Error:` | **Tell** the user what went wrong, then **stop** polling |

> **CRITICAL:** When you receive plain text progress/reasoning lines from `working`:
> 1. Read the tool result
> 2. Output that exact text as your own chat message (not as a tool result display)
> 3. Do NOT rephrase, summarize, or add commentary
> 4. Display each line exactly as written
> 5. Then call `working` again
>
> This ensures progress appears in the chat window, not buried in tool execution chips.
> These are CLAIRE's live execution progress and internal reasoning — users need to
> see them in the main conversation flow.

> **Important:** `working` always returns ONE type of response per call — either
> plain text lines OR the final `ANSWER:`, never both together.
> The `ANSWER:` line is always the entire content of its response, making it
> safe to parse the JSON directly from that line.

---

## Example Workflow

Here's exactly how to handle `working` responses:

**Scenario:** User asks "show me customer tables"

```
1. Call: OrchestratorSkill("show me customer tables")
   Result: "CLAIRE is working..."
   
2. Call: working()
   Result: "Mapping identified concepts to your metadata..."
   Your response: "Mapping identified concepts to your metadata..."
   (Output the text as your own message, then continue)
   
3. Call: working()
   Result: "Generating queries..."
   Your response: "Generating queries..."
   (Output the text as your own message, then continue)
   
4. Call: working()
   Result: "Looking for Table assets..."
   Your response: "Looking for Table assets..."
   (Output the text as your own message, then continue)
   
5. Call: working()
   Result: "ANSWER: {\"responseType\":\"FINAL_BUNDLE\",...}"
   Your response: [Render the artifact per Step 3 below]
```

**Key point:** Steps 2-4 show progress text appearing as YOUR messages in the chat, not trapped in tool result boxes.

---

## Step 3 — Render the `ANSWER:`

When `working` returns a response whose first line is `ANSWER:`, the entire response
is that single line. Extract and parse the JSON like this:

```
response  = <what working() returned>
json_text = response[len("ANSWER: "):]   # strip the 7-char prefix
bundle    = JSON.parse(json_text)
```

The top-level object has this shape:
```json
{
  "responseType": "FINAL_BUNDLE",
  "artifact":    { ... } | null,
  "plan":        { ... } | null,
  "human_probe": { ... } | null
}
```

### Handling each field

**`artifact`** — The main answer to show the user. Contains `payload.data[]`, an array
of view objects. Render each one in order (see Artifact Rendering below).

**`plan`** — CLAIRE's execution plan (`payload.plan`). Treat as internal context only —
do not display it to the user.

**`human_probe`** — A clarification question from CLAIRE (`payload.probe_message`).
Present it to the user verbatim and wait for their answer before proceeding.
If `working` returns a line starting with `PROBE:`, that is the same probe question
surfaced for easy detection — display the text after "PROBE: " verbatim to the user.

**PLAN vs HUMAN_PROBE:** A `PLAN` describes what CLAIRE intends to do — keep it as
context. A `HUMAN_PROBE` is a direct question for the user — always surface it verbatim.

After rendering the artifact, add one brief factual summary line
(e.g. "Found 12 customer tables in RETAIL schema." or "Data quality score for ORDERS is 74%.").
Keep it factual and concise — do not rephrase or expand the data.

---

## Artifact Rendering

Read `artifact.payload.data[]` and render each entry based on its `viewType`:

### GridView — tabular search or discovery results in a fancy representational layout
- Build column headers from `schema_definitions[].label` in order
- Map each row in `items[]` by `fieldName` position
- If `key_insights` is present, display it below the table as a callout

### null or DataExploreResult — data preview with SQL in a fancy representational layout
- Build table: `schema_definitions[].label` as headers, `items[][]` rows by position
- Render `null` as `—`
- Show the SQL below the table:
  ````sql
  <execution_context.generated_sql>
  ````
- Footer line: `Rows: <row_count_total> | Execution time: <execution_time_ms>ms`

### PropertiesView — asset metadata and properties in a fancy representational layout
- Two-column table: **Property** | **Value**
- If multiple property groups exist, use subheadings to separate them

### GraphView — lineage and relationships in a fancy representational layout
- Use `→` arrows to show direction: `SourceTable → TransformLayer → TargetTable`
- One node per line; indent branches for hierarchy

### AnalyticsView — metrics and aggregated data in a fancy representational layout
- Lead with the most important metric in bold
- Highlight trends and comparisons; keep commentary minimal

### TreeView — hierarchical structures (catalogs, schemas, folders)
```
📁 Database
  └── 📂 Schema
        ├── 🗂️ Table A
        └── 🗂️ Table B
```

### BlockText — plain prose
- Display as-is

---

## Error & Empty States
- Empty result → *No results found. Try rephrasing your query.*
- Error in response → *Encountered an error — please try again.*