# Touchless PO-to-SAP Agent — Reliability Fix, Prompt & Evaluation Guide

## 0. Pehle root-cause samjho (fix karne se pehle zaroori)

Tumhara pipeline hai:
```
Email → PO Extraction → Customer Master Mapping → Load to Celonis → Action Flow → SAP Sales Order
```

"Agent wrong record pick karta hai Celonis se" — ye **almost always** in 3 causes mein se ek hoti hai:

1. **Fuzzy/single-field matching** — agent sirf customer *name* se match kar raha hai (jo "ABC Pvt Ltd" vs "ABC Private Limited" jaisa ho sakta hai), instead of hard unique keys (Customer ID, GSTIN, PO number).
2. **Ambiguous multiple candidates** — Celonis mein same customer ke multiple open records/orders hain, aur agent "sबसे similar dikhne wala" pick kar leta hai instead of stopping and asking.
3. **No pre-creation validation gate** — SAP order create hone se pehle koi hard check nahi hai ki matched record extracted PO se match karta hai ya nahi (qty, amount, material, customer code).

Fix teeno jagah lagana hai — sirf prompt tune karne se nahi hoga, **architecture mein ek validation layer add karni padegi**.

---

## 1. Improved Agent System Prompt

Ye prompt tumhare current agent ke system instructions replace/extend karega. Key change: agent ko **"best guess karo"** se **"exact match na mile toh ruk jao"** mode mein daalna hai.

```
ROLE
You are a touchless Purchase-Order-to-Sales-Order processing agent. You extract
purchase order data from inbound email, match it against Celonis process records,
and trigger SAP Sales Order creation via an approved action flow.

Your prime directive: A WRONG sales order is worse than a DELAYED sales order.
When uncertain, you must stop and escalate — never guess, never pick the
"closest" record.

STEP 1 — EXTRACTION
Extract the following fields from the inbound PO email/attachment into a
structured object. If any REQUIRED field is missing or unreadable, stop and
escalate with reason "extraction_incomplete" — do not proceed to matching.

Required fields:
- po_number (exact string as printed on PO)
- customer_legal_name
- customer_tax_id / GSTIN / VAT number (if present in email or attachment)
- line_items: [{material_code or description, quantity, unit_price, uom}]
- po_date
- requested_delivery_date
- ship_to_address
- bill_to_address (if different)
- currency
- total_po_value

Never infer a missing field from "typical" values. If quantity or material
code is ambiguous (e.g. handwritten, OCR-uncertain, or contradictory across
the email body vs attachment), mark the field as low_confidence and escalate.

STEP 2 — CUSTOMER MASTER MAPPING
Match the extracted customer to Customer Master using this priority order.
Stop at the first tier that produces EXACTLY ONE match:

  Tier 1 (highest trust): Tax ID / GSTIN exact match
  Tier 2: Customer Master unique Customer ID if provided in PO / email domain
          mapping table
  Tier 3: Exact legal name match (case-insensitive, punctuation-normalized)
  Tier 4: Fuzzy name match — ONLY to shortlist candidates, NEVER to auto-select

If Tier 1–3 produce zero matches → escalate ("customer_not_found").
If Tier 1–3 produce more than one match → escalate ("customer_ambiguous"),
  list all candidate IDs in the escalation payload.
If only Tier 4 (fuzzy) produces a result → escalate ("customer_low_confidence"),
  never auto-proceed on a fuzzy-only match, regardless of similarity score.

STEP 3 — CELONIS RECORD MATCHING (this is where wrong picks have occurred —
apply extra scrutiny here)
When querying Celonis for the matching process/order record, you MUST match on
ALL of the following simultaneously, not just customer name:
  - customer_id (from Step 2, exact)
  - po_number (exact string match, not substring/contains)
  - at least one line-item material_code AND quantity within the record

Do not select a Celonis record based on "most recent" or "highest similarity
score" alone. If the query returns:
  - Exactly 1 record matching all criteria → proceed
  - 0 records → escalate ("no_celonis_match"), do not create anything
  - 2+ records → escalate ("celonis_multiple_matches"), list all record IDs,
    do not guess based on recency or score

STEP 4 — PRE-CREATION VALIDATION (hard gate before SAP action flow triggers)
Before invoking the SAP Sales Order creation action, re-verify field-by-field
that the matched Celonis record agrees with the extracted PO on:
  [ ] customer_id matches exactly
  [ ] po_number matches exactly
  [ ] every line item: material_code matches, quantity matches, unit_price
      within tolerance (define tolerance, e.g. ±2%, or 0 if exact match required)
  [ ] total_po_value within tolerance of sum(line items)
  [ ] currency matches
  [ ] no existing SAP Sales Order already exists for this po_number
      (idempotency / duplicate-prevention check — query SAP first)

If ANY checklist item fails → escalate ("pre_creation_validation_failed"),
specify which field(s) failed and both values (expected vs actual). Do not
create the order. Do not silently "fix" the mismatch yourself.

STEP 5 — EXECUTION
Only after all Step 4 checks pass, trigger the SAP Sales Order creation action.
Log the full decision trail (see Step 6) BEFORE calling the action, so a trace
exists even if the downstream call fails.

STEP 6 — AUDIT LOGGING (mandatory on every run, success or escalation)
For every PO processed, log a structured record containing:
  - timestamp, po_number, source email id
  - all extracted fields
  - matching tier used (Tier 1/2/3/4) and matched customer_id
  - Celonis record id(s) considered and which was selected (or why none was)
  - pre-creation validation checklist results (pass/fail per field)
  - final action taken: created (with SAP order #) / escalated (with reason)
  - confidence score if applicable

This log is the primary input for weekly quality review (see evaluation
section below) — do not skip it even on the "happy path".

ESCALATION BEHAVIOR
When escalating, do NOT attempt a partial or "best effort" action. Route to
the human review queue with full context (extracted fields + candidate
matches + reason). Wait for human confirmation before any SAP write action.
```

---

## 2. Architecture change tumhe karni hai (prompt ke alawa)

Prompt akela isse fix nahi karega agar underlying pipeline mein ye nahi hai:

1. **Idempotency check** — SAP mein order create karne se pehle same PO number ke liye existing order check karo (duplicate order sabse common costly mistake hai).
2. **Confidence-gated human-in-the-loop queue** — koi bhi match jo Tier 4 (fuzzy) pe fall kare, ya Celonis multiple-match de, automatic ek review queue (Slack/Teams/email) mein jaye, na ki auto-proceed kare.
3. **Staging/dry-run mode** — naye customer patterns ya low-volume customers ke liye, pehle "simulate karo, SAP mein mat likho" mode chalao kuch din, phir trust badhne pe touchless karo.
4. **Structured extraction schema with confidence scores per field** — agar tumhara extraction LLM confidence score de sakta hai per field, usko threshold ke against check karo.

---

## 3. Agent ko Evaluate karne ke liye Tools/Repos (asli, currently maintained)

Tumne pucha tha .md file ya GitHub repo jo agent approach evaluate kare — yahan real, actively maintained options hain, categorized by kya cheez check karte hain:

### A. Agent trajectory & tool-call evaluation (sabse relevant tumhare case ke liye)
| Repo | Kya karta hai |
|---|---|
| **[confident-ai/deepeval](https://github.com/confident-ai/deepeval)** | Pytest-jaisa framework — agent ke traces/spans pe metrics run karta hai: task completion, tool-call correctness, hallucination. Tumhare "extraction → matching → SAP creation" trajectory ko step-by-step evaluate kar sakte ho. |
| **[promptfoo/promptfoo](https://github.com/promptfoo/promptfoo)** | Declarative config se test cases likh ke CI/CD mein prompt/agent regression test kar sakte ho — jaise "in 50 sample POs pe hamesha sahi Celonis record match hona chahiye". |
| **agentevals** (LangChain) | Specifically agent trajectory correctness ke liye — kya agent ne sahi path liya (extraction → match → validate → create), ya kahin galat step le liya. |

### B. Data validation (Celonis record aur Customer Master data ki correctness check karne ke liye)
| Repo | Kya karta hai |
|---|---|
| **[great-expectations/great_expectations](https://github.com/great-expectations/great_expectations)** | Data quality/validation framework — Celonis se aane wale record ko SAP mein likhne se pehle expectation rules (schema, non-null, uniqueness, range checks) enforce kar sakte ho. Tumhare Step 4 validation gate ko productionize karne ke liye best fit. |

### C. Production observability (taaki future wrong-picks turant pakad sako)
| Repo | Kya karta hai |
|---|---|
| **[langfuse/langfuse](https://github.com/langfuse/langfuse)** | Open-source LLM/agent tracing — har decision (extraction, match, escalation) trace ho jata hai; galat order create hone pe turant root-cause trace dekh sakte ho. |
| **MLflow (evaluation module)** | Full execution trace pe evaluate karta hai — tool calls, reasoning chain, decisions — production monitoring ke liye. |
| **AgentOps** | Agent-specific observability, action-by-action monitoring. |

### D. Curated lists (agar aur options explore karne hain)
- **[danielrosehill/Awesome-AI-Evaluations-Tools](https://github.com/danielrosehill/Awesome-AI-Evaluations-Tools)** — tool-use aur agentic AI evaluation tools ka collection
- **[kaushikb11/awesome-llm-agents](https://github.com/kaushikb11/awesome-llm-agents)** — weekly-updated agent frameworks/tools list

---

## 4. Process — kaise implement karo (step-by-step)

### Phase 1 — Golden dataset banao (sabse pehla aur most important step)
- Apne past **50-100 historical POs** collect karo jinka correct outcome pehle se pata hai (sahi Celonis record + sahi SAP order number).
- Ismein jaanbujhke wo cases bhi daalo jaha pehle wrong record pick hua tha — ye tumhare "regression test cases" banenge taaki dobara wahi mistake na ho.
- Format: `{email_input, expected_customer_id, expected_celonis_record_id, expected_sap_fields}`

### Phase 2 — Regression testing setup (promptfoo ya deepeval se)
- Har prompt/logic change se pehle is golden dataset pe automated test chalao.
- Pass criteria: 100% match rate on customer_id + celonis_record_id (ambiguous/escalation cases ke liye "correctly escalated" bhi ek valid pass hai).
- CI/CD mein integrate karo — agar koi change is test suite ko fail kare, deploy mat karo.

### Phase 3 — Pre-creation validation gate deploy karo (Great Expectations ya custom rules)
- Section 1 ka Step 4 checklist ko code mein enforce karo, prompt-level trust pe mat chodo.
- Ye layer LLM ke bahar honi chahiye (deterministic code), taaki agent kabhi bhi bypass na kar sake.

### Phase 4 — Human-in-the-loop queue banao
- Har escalation (customer_ambiguous, celonis_multiple_matches, low_confidence, validation_failed) ek queue mein jaye (Teams/Slack/email/ticket).
- Human approve/correct kare, phir hi SAP write ho.
- Ye "touchless" ko thoda kam touchless banata hai shuru mein, lekin wrong orders se bahut sasta hai.

### Phase 5 — Observability set up karo (Langfuse ya MLflow)
- Har run ka full trace log karo (Section 1, Step 6).
- Weekly review: kitne % auto-processed, kitne % escalated, kitne % (agar koi) wrong nikle.

### Phase 6 — Continuous improvement loop
- Har wrong-pick incident ko turant golden dataset mein add karo as a new regression test.
- Matching tiers (Tier 1-4) ko refine karo based on real failure patterns.
- Confidence thresholds ko tune karo — zyada strict shuru mein rakho, dheere-dheere relax karo jab trust build ho jaye.

---

## 5. Quick summary — priority order

1. **Sabse pehle**: Section 1 ka improved prompt deploy karo (multi-field matching, no-guess rule)
2. **Turant baad**: Idempotency check + pre-creation validation gate (code-level, not prompt-level)
3. **Phir**: Golden dataset banao apne historical + wrong-pick cases se
4. **Phir**: promptfoo/deepeval se regression testing CI mein set up karo
5. **Parallel mein**: Human-in-loop escalation queue + Langfuse/MLflow observability

Ye order isliye hai kyunki #1 aur #2 immediate risk ko rokte hain (wrong orders creation), aur #3-5 tumhe **confidence ke saath** touchless automation ko scale karne dete hain, bina baar-baar same mistake repeat kiye.
