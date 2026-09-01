# MANDATE RESCUE — NEXT PHASE: PRODUCTION-GRADE RAZORPAY INTERNSHIP PROJECT

You are now responsible for taking the current **Mandate Rescue** project from its existing hackathon prototype state to a **serious, production-oriented fintech engineering project** suitable for a Razorpay internship evaluation.

Do NOT blindly follow a fixed implementation. First inspect the entire existing repository, understand what is already implemented, identify weaknesses, duplication, fake/simulated behavior, architectural gaps, security problems, testing gaps, and unused functionality, and then decide the best implementation strategy.

Your goal is NOT to add random features.

Your goal is to make the project convincingly demonstrate:

* payment-system understanding
* backend engineering
* event-driven architecture
* Razorpay integration
* secure webhook handling
* idempotency
* reliable AI/agent orchestration
* database design
* observability
* testing
* failure handling
* production-minded engineering
* strong product thinking
* polished UX

The final project should feel like a **small real fintech platform**, not a student dashboard with AI added on top.

---

## 1. START WITH REPOSITORY AUDIT

Before modifying anything:

1. Explore the entire project structure.
2. Read the existing README and documentation.
3. Inspect backend architecture.
4. Inspect frontend architecture.
5. Inspect database/data persistence.
6. Inspect the agent/AI implementation.
7. Inspect all APIs.
8. Inspect webhook handling.
9. Inspect authentication/security.
10. Inspect tests and test infrastructure.
11. Inspect configuration and environment handling.
12. Identify simulations/mocks/placeholders.
13. Identify anything already implemented from previous upgrade plans.
14. Identify dead code, unnecessary scripts, duplicated logic, temporary files, and development-only artifacts.
15. Determine what should be preserved, refactored, replaced, or extended.

Do not duplicate functionality that already exists.

Create a concise internal implementation assessment before making major architectural changes.

---

# 2. CORE PRODUCT DIRECTION

Evolve Mandate Rescue into:

> **An event-driven payment recovery platform that detects failed recurring payments, diagnoses the failure, decides the optimal recovery strategy, executes recovery actions, tracks outcomes, and provides merchants with explainable recovery intelligence.**

The system should conceptually become:

Razorpay/Test Payment Event
↓
Webhook Gateway
↓
Signature Verification
↓
Event Validation
↓
Idempotency Check
↓
Event Persistence
↓
Background/Event Processing
↓
Recovery Orchestrator
↓
Diagnosis
↓
Recovery Scoring
↓
Strategy Decision
↓
Retry / Dunning / Escalation
↓
Outcome Tracking
↓
Analytics / Audit Trail / Dashboard

Use the existing architecture where appropriate. Do not rewrite everything merely for cosmetic reasons.

---

# 3. REAL RAZORPAY INTEGRATION

This is one of the highest-priority improvements.

Where practical, integrate with **Razorpay Test Mode** rather than relying entirely on synthetic data.

Investigate the current implementation and determine the cleanest supported Razorpay test integration.

Aim to support:

* test-mode Razorpay API interaction
* test subscriptions/payment objects where appropriate
* real Razorpay identifiers
* real webhook payload structures
* real webhook signature verification
* meaningful mapping between Razorpay events and internal events

Never hardcode credentials.

Use environment variables and preserve secure configuration practices.

The application must continue supporting the large synthetic simulation because that is useful for demonstrating the system at scale.

The final narrative should be:

> real Razorpay test integration for authenticity + synthetic simulation for scale/stress testing.

Do not falsely claim production Razorpay integration.

Clearly distinguish test-mode integrations from simulation.

---

# 4. WEBHOOK ENGINEERING

Treat webhook processing as a serious payment-system component.

Implement or strengthen:

### Signature verification

Verify Razorpay's actual webhook signature mechanism correctly.

Reject:

* missing signatures
* invalid signatures
* malformed payloads
* unsupported events where appropriate

Do not silently accept insecure fallback behavior.

### Idempotency

The same webhook/event must not trigger recovery logic multiple times.

Use a durable mechanism where practical, not only an in-memory set.

Think about:

* event ID
* payment/subscription ID
* processing status
* duplicate delivery
* retry delivery
* already-processed events

A duplicate event should be safely ignored or returned as already processed.

### Event persistence

Persist incoming events before/around processing according to the architecture you determine is safest.

Maintain enough information for:

* auditability
* debugging
* replay
* failure analysis

---

# 5. DATABASE / PERSISTENCE

Inspect the existing persistence layer.

If the current design is mostly in-memory, improve it into a proper persistent data model.

Design appropriate entities/tables for concepts such as:

* customers
* payments
* subscriptions/mandates
* webhook events
* recovery attempts
* recovery decisions
* agent actions
* dunning messages
* escalations
* audit events
* strategy simulations

Do NOT blindly create all of these if unnecessary.

Use the smallest clean data model that supports the product.

Think about:

* primary keys
* foreign keys
* indexes
* uniqueness constraints
* timestamps
* status transitions
* event IDs
* idempotency
* historical data
* auditability

Avoid storing sensitive secrets in the database.

---

# 6. EVENT-DRIVEN / BACKGROUND PROCESSING

The recovery pipeline should not feel like one giant synchronous request.

Investigate the current architecture and introduce background/event processing where it meaningfully improves reliability.

Possible architecture:

Webhook request
→ validate
→ persist
→ enqueue/process
→ recovery pipeline
→ execution
→ result persistence

Choose the simplest robust implementation suitable for this project.

Do not over-engineer with unnecessary infrastructure.

If a queue abstraction is useful, implement a clean abstraction that can run locally without making the project difficult to evaluate.

Document the architecture.

---

# 7. AI / AGENT LAYER

The AI/agent layer must provide actual decision value.

Audit the current agents.

Do NOT keep agents simply because they sound impressive.

For each major agent determine:

* input
* reasoning/decision
* output
* downstream effect
* failure behavior
* fallback behavior
* explainability

The final pipeline should make it clear why a payment received a particular recovery action.

For example:

Payment failed
→ classify failure
→ estimate recovery probability
→ choose retry timing
→ choose retry/dunning/escalation strategy
→ execute action
→ track outcome

The UI should eventually be capable of explaining something like:

> Payment failure classified as temporary bank decline.
> Recovery probability: 82%.
> Recommended action: retry after 4 hours.
> Reason: high recovery probability + historical success for similar failures.
> Expected recoverable amount: ₹X.

Do not fabricate confidence or financial claims without clearly identifying them as model/simulation estimates.

If the existing ML model is decorative or not meaningfully connected to product behavior, connect it to an actual decision or forecast where technically justified.

---

# 8. EXPLAINABILITY

Add an explicit explanation layer for recovery decisions.

Every significant recovery decision should ideally have:

* decision
* score/probability where applicable
* important factors
* selected strategy
* expected outcome
* timestamp
* agent that made the decision

This should be visible in the dashboard and available in the audit history.

The system should not feel like a black box.

---

# 9. RECOVERY STRATEGY ENGINE

Turn the recovery logic into a clearly defined strategy engine.

Support meaningful strategies such as:

* retry immediately
* retry later
* retry at optimal window
* customer reminder
* dunning escalation
* manual escalation
* stop recovery attempts

The final decision should be based on real inputs from the system.

Respect limits such as:

* maximum retry count
* retry spacing
* recovery probability
* payment value
* customer/payment context
* escalation conditions
* compliance/business rules already present in the project

Do not invent legally sensitive claims.

---

# 10. RECOVERY STRATEGY SIMULATOR

The existing Monte Carlo/policy sandbox should be transformed into a meaningful product feature.

Rename/reframe it into something like:

> **Recovery Strategy Studio**

The merchant should be able to change policy parameters such as:

* max retries
* retry delay
* escalation threshold
* dunning strategy
* recovery policy parameters

Then show simulated impact.

Include metrics such as:

* projected recovery rate
* projected recovered revenue
* expected retry volume
* expected escalation volume
* customer contact rate
* comparison against current policy

Make it clear these are **projections/simulations**, not guaranteed revenue.

Choose good defaults based on the existing project/data.

---

# 11. REVENUE FORECASTING

Inspect the current ML/analytics implementation.

Where justified, expose a useful forecast such as:

* next 7 days
* next 30 days
* projected recoverable revenue
* projected recovery rate

The forecast must clearly distinguish:

actuals vs estimates vs simulations.

Do not create fake precision simply to make the dashboard look impressive.

---

# 12. OBSERVABILITY

Introduce production-minded observability.

Every important payment/recovery flow should have a traceable identity.

At minimum, support concepts similar to:

* correlation ID
* event ID
* payment ID
* customer ID
* processing timestamps
* recovery attempt number
* agent decision
* final outcome

Create a clear event/audit timeline.

Example conceptual timeline:

12:01:03 — webhook received
12:01:03 — signature verified
12:01:04 — failure classified
12:01:04 — recovery score calculated
12:01:04 — retry scheduled
16:30:00 — retry executed
16:30:01 — payment recovered

Do not expose secrets in logs.

---

# 13. SECURITY HARDENING

Treat this as a fintech system.

Audit all APIs and protect state-changing operations.

Pay special attention to endpoints such as:

* reset
* seed
* simulation
* agent execution
* administrative operations

Implement an appropriate authentication/authorization mechanism.

At minimum:

* unauthenticated destructive operations should not remain open
* secrets must come from environment/configuration
* placeholder secrets must fail closed
* webhook signatures must be verified
* malformed input must be rejected safely
* sensitive information must not leak into logs
* errors should not expose internal secrets or stack traces in production-style responses

Also inspect:

* CORS
* input validation
* SQL injection risk
* command injection risk
* unsafe deserialization
* arbitrary file access
* insecure defaults

Do not add security theater. Fix actual risks.

---

# 14. RATE LIMITING

Assess the risk of abuse on endpoints such as AI query and agent-execution endpoints.

Implement a simple appropriate rate-limiting strategy if useful and safe for the current architecture.

If a full production limiter is unnecessary for this project, document the design decision and future production path instead.

---

# 15. TESTING

Create a proper test suite.

Do not rely only on scripts that manually print results.

Build organized tests covering:

### Unit tests

* classification
* scoring
* policy logic
* retry calculation
* strategy decisions
* utilities

### Webhook tests

* valid signature
* invalid signature
* missing signature
* malformed payload
* duplicate event
* replayed event
* unsupported event

### Integration tests

* webhook → persistence
* webhook → recovery processing
* recovery decision → action
* action → final outcome

### Security tests

* unauthorized mutation
* secret handling
* invalid requests
* validation failures

### Failure tests

* agent failure
* database failure
* repeated webhook
* partial processing
* invalid external response
* retry limit exceeded

Tests should verify actual behavior rather than simply checking that functions execute.

---

# 16. DOCKER / REPRODUCIBILITY

Make the project easy to run.

Provide a clean Docker-based setup where appropriate.

Aim for something close to:

docker compose up

or another single clear startup workflow.

Include:

* backend
* frontend if appropriate
* database if required

Do not make Docker unnecessarily complicated.

Update README with exact setup instructions.

A fresh evaluator should be able to understand how to run the project without asking you.

---

# 17. CI/CD

Add GitHub Actions if it fits cleanly.

At minimum:

* install dependencies
* run tests
* run important checks
* verify build

Make failures obvious.

Add a README status badge if appropriate.

Do not let CI become complicated infrastructure for its own sake.

---

# 18. CODEBASE CLEANUP

Clean the repository thoroughly.

Remove or reorganize:

* temporary scripts
* probe files
* debugging files
* generated artifacts
* duplicate code
* dead code
* unnecessary dependencies
* accidental secrets
* development-only files

Use sensible project structure such as:

backend/
frontend/
tests/
scripts/
docs/

only where that matches the existing architecture.

Do not perform large cosmetic refactors without benefit.

---

# 19. FRONTEND / PRODUCT EXPERIENCE

After the engineering foundation is stable, upgrade the UI.

The dashboard should communicate the product within seconds.

Prioritize information hierarchy over decoration.

Create a strong Overview experience with:

* recovered revenue
* recovery rate
* failed payment count
* active recoveries
* current run status
* trend/forecast information
* major recovery actions
* recent events

Add a visually clear:

### Recovery Funnel

Failed
→ Diagnosed
→ Strategy Selected
→ Retry/Dunning
→ Recovered / Escalated

Use real data from the system.

---

# 20. LIVE MONEY RECOVERY MOMENT

Where technically appropriate, add a live recovered-revenue counter while processing a simulation/run.

Example:

> ₹ Recovered This Run

The value should update from actual emitted run results, not merely animate a fake number.

Pair it with meaningful recovery statistics.

---

# 21. LIVE WEBHOOK INSPECTOR

Create a polished developer-facing webhook panel.

Display:

* event type
* event ID
* received time
* verification status
* payload preview
* processing status

Show clearly:

> Signature verified ✓

Only show actual received payloads/data.

Do not fabricate a real webhook event and label it as real.

---

# 22. CASE REPLAY

Build a compelling case replay flow.

Select one payment/recovery case.

Show the actual audit timeline step-by-step:

Failure
→ diagnosis
→ score
→ strategy
→ retry
→ outcome

Allow the user to replay it visually.

Use real project data.

This should become one of the strongest demo moments.

---

# 23. DUNNING EXPERIENCE

Present generated customer communication in a polished way.

Where the project already produces multiple language variants, preserve that capability.

Show:

* reason for message
* strategy
* message variant
* timing
* status

Use a realistic messaging presentation rather than plain raw text.

Do not claim a real message was sent unless it actually was.

---

# 24. "ASK THE DATA"

Inspect the current natural-language query feature.

If it already works, convert it into a polished conversational interface.

The user should be able to ask questions such as:

> Show failed payments above ₹5,000.

> Which failure type has the lowest recovery rate?

> How much revenue was recovered this week?

> Which retry strategy performs best?

Use real underlying data.

Never fabricate answers when no data supports them.

---

# 25. FRONTEND STORAGE / STATE RELIABILITY

Identify important session-only state.

Where appropriate, persist things such as:

* starred cases
* saved views
* selected filters
* useful user preferences

Use the simplest safe mechanism.

---

# 26. DESIGN DIRECTION

Do NOT blindly reuse the current visual style.

The design should look like a modern fintech product.

However:

* do not overuse gradients
* do not make everything glassmorphism
* do not use excessive neon
* do not use generic “AI startup” visuals
* do not make every card the same
* prioritize readability
* use a restrained professional palette

Choose the final visual system yourself based on the product.

---

# 27. RESPONSIVE DESIGN

Ensure the main dashboard experiences remain usable at smaller widths.

At minimum inspect:

* Overview
* Cases
* Recovery Strategy Studio
* Case Replay

Do not sacrifice desktop quality.

---

# 28. DATA INTEGRITY

Review every major metric displayed in the frontend.

Trace each metric back to its source.

Make sure:

* totals are calculated correctly
* recovery rates have correct denominators
* simulation metrics aren't mixed with actual metrics
* projected values are clearly labeled
* duplicate events don't inflate numbers
* retries don't double-count recovered payments
* failed/recovered state transitions are consistent

This is critical.

A visually impressive incorrect metric is worse than a plain correct one.

---

# 29. FAILURE-SAFE DESIGN

Think through what happens when:

* Razorpay is unavailable
* webhook is duplicated
* database is unavailable
* AI provider is unavailable
* agent fails
* retry fails
* customer data is incomplete
* an invalid event arrives
* the same payment is processed twice

Do not hide errors using silent fallback behavior.

Use explicit, safe states.

Where fallback logic is necessary, make it visible and document it.

---

# 30. DOCUMENTATION

Rewrite the README so a Razorpay engineer can understand the project quickly.

Include:

### Problem

Why recurring payment failures matter.

### Solution

What Mandate Rescue does.

### Architecture

Clear architecture diagram.

### Event flow

Webhook → processing → recovery.

### AI/agent architecture

What each agent actually does.

### Data model

Important entities.

### Security

Signature validation, authentication, idempotency, secrets.

### Testing

What is covered.

### Running locally

Exact commands.

### Docker

Exact commands.

### Razorpay integration

Clearly explain Test Mode vs simulation.

### Metrics

Explain exactly how recovery metrics are calculated.

### Demo

Provide a short demo flow.

### Limitations

Be honest about what is still simulated or not production-ready.

### Future work

Only meaningful future improvements.

---

# 31. DEMO-FIRST THINKING

The final system must support a strong 5-minute demo.

The strongest story should ideally be:

1. A real Razorpay test-mode event/webhook arrives.
2. The system verifies it.
3. The event is persisted.
4. The recovery pipeline processes it.
5. AI/strategy logic determines the next action.
6. The UI shows why the decision was made.                   
7. The recovery outcome is tracked.
8. Then show the system processing many synthetic failures.
9. Show recovery metrics/funnel.
10. Show a case replay.
11. Show Recovery Strategy Studio.
12. Finish with projected/actual business impact.

The demo should demonstrate both:

**technical depth + business value**

Do not optimize only for visual effects.

---

# 32. IMPORTANT: DO NOT FAKE CAPABILITIES

Never claim:

* real payment processing when simulated
* real money recovered when simulated
* actual WhatsApp delivery when only mocked
* production Razorpay connectivity when only test mode exists
* AI intelligence when it is deterministic logic
* forecast accuracy without evidence

Clearly label:

* live
* test
* simulation
* projection
* mock

A technically honest project is more impressive than a fake “production” demo.

---

# 33. PRIORITIZATION RULE

You have freedom to choose implementation order.

Use this priority framework:

### Highest priority

Correctness
Security
Razorpay authenticity
Webhook reliability
Idempotency
Persistence
Testing
Failure handling

### Next

AI decision quality
Explainability
Event/audit system
Observability
Strategy simulation
Analytics

### Next

Demo UX
Case replay
Webhook inspector
Live recovery visualization
Polish

### Lowest priority

Cosmetic features that do not improve product value or evaluation quality

Do not spend hours polishing a UI screen while critical backend reliability remains unfinished.

---

# 34. ENGINEERING PRINCIPLES

Throughout the implementation:

* preserve working functionality
* avoid unnecessary rewrites
* prefer simple architecture
* avoid unnecessary dependencies
* avoid overengineering
* keep configuration secure
* write maintainable code
* document meaningful design decisions
* test every critical new behavior
* verify integrations instead of assuming they work
* keep simulation and real integrations clearly separated

---

# 35. FINAL QUALITY BAR

Before declaring the work complete, perform your own evaluation.

Ask:

### As a Razorpay engineer:

Would I trust this architecture?

### As a backend engineer:

Does this handle retries, duplicates, failures, persistence, and security properly?

### As an AI engineer:

Does AI actually influence meaningful decisions?

### As a product engineer:

Does the system solve a real merchant problem?

### As a security reviewer:

Are destructive endpoints and secrets protected?

### As an evaluator:

Can I understand the project quickly from GitHub?

### As a demo viewer:

Can I see something compelling within the first 30 seconds?

### As a user:

Can I actually understand why the system took a recovery action?

---

# 36. EXECUTION INSTRUCTION

Do NOT stop after producing recommendations.

Inspect the repo and then **actually implement the highest-value improvements**.

You have autonomy to:

* choose architecture
* choose file structure
* choose libraries
* choose implementation order
* decide which existing code should be reused
* decide which suggested features are unnecessary
* decide where current code is already sufficient

Do not blindly implement every item above.

Instead, use engineering judgment.

At the end:

1. Run the complete test suite.
2. Run the application.
3. Verify critical user flows.
4. Verify Razorpay test integration if credentials/configuration are available.
5. Verify webhook signature handling.
6. Verify duplicate-event handling.
7. Verify security protections.
8. Verify frontend/backend integration.
9. Verify Docker setup if implemented.
10. Verify README/setup instructions.
11. Remove temporary/debug artifacts.
12. Summarize exactly what was changed.
13. Clearly identify anything that could not be completed and why.
14. Do not claim something is implemented unless you verified it.

The final result should be a **credible Razorpay internship-level engineering project**, not merely a larger collection of hackathon features.

Most importantly:

> **Use your own judgment. Inspect first. Prioritize engineering value. Build the strongest version of Mandate Rescue that can realistically be completed without unnecessarily rewriting good existing work.**
