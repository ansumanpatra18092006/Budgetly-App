# FraudShield production architecture and risk methodology

## Architecture
FraudShield separates the institution-owned fraud transaction stream from consumer personal-finance and lending data. Ingestion normalizes events, enforces replay/idempotency controls, enriches behavioral context, computes ML/rule/behavior/network risk, applies the institution response policy, persists audit evidence, and queues long-running notifications. A separate worker claims durable jobs with PostgreSQL `FOR UPDATE SKIP LOCKED`.

## Risk methodology and false positives
The final score fuses supervised XGBoost, configurable rules, behavioral deviation and network risk. A high score is an investigation signal rather than proof of fraud. Explanations expose rule contributors and model evidence. Institutions can tune thresholds. Human analysts control case disposition and all external cybercrime reporting. A same-amount normal-account example is retained to demonstrate contextual false-positive reduction.

## Security model / threat scenarios
Controls cover server-side lender authorization, institution data isolation, secure cookies, restrictive CORS, security headers, API rate limiting, payment-webhook authentication, replay/idempotency rejection, audit trails, and secret configuration through environment variables. Threat scenarios include broken authorization, API abuse, sensitive-data leakage, forged/replayed payment events, payment manipulation, malicious input and AI prompt injection. Copilot is evidence-grounded and instructed not to invent evidence or declare fraud as fact.

## Failure recovery
Database connections retry short transient network failures. Webhook replay fingerprints prevent duplicate financial events. Durable asynchronous jobs have idempotency keys, bounded exponential retry, dead-letter state and stale-job recovery. Readiness is separate from liveness, so an unhealthy database can remove an instance from traffic without claiming the process is dead.

## Observability
Every request receives a request ID, structured request log, latency measurement and aggregate counters. `/health` is liveness, `/ready` validates the database and `/metrics` provides aggregate request/error/latency counters. FraudShield exposes queue, replay and audit counts through its lender-only operations endpoint.

## Scalability
Stateless web processes can scale horizontally behind a load balancer. PostgreSQL indexes cover institution/time, account/time, investigations and audit scans. Durable jobs use SKIP LOCKED for multiple workers. Intelligence caching avoids repeated scoring work. At larger scale the same queue contract can be moved to Kafka/SQS/PubSub and aggregate metrics to OpenTelemetry/Prometheus without changing the fraud domain model.

## AI evaluation
`/lender/fraudshield/intelligence/evaluation` runs a deterministic synthetic red-team benchmark and reports accuracy, precision, recall, false-positive/false-negative rate, latency, explainability coverage, safety controls and cost. The benchmark is explicitly labeled synthetic and is not represented as production validation.

## Deployment
`Dockerfile` and `docker-compose.yml` run the web service and fraud worker. `render.yaml` provides an alternative cloud deployment template. GitHub Actions compiles, tests, runs security/dependency checks and builds the container image.

## External integrations
A payment-event webhook and a server-side verified-transaction callback are implemented. Real production/sandbox settlement verification still requires credentials from the selected payment or banking provider; secrets must never be committed to source control. SOC delivery similarly requires `FRAUDSHIELD_SOC_WEBHOOK_URL` and an explicit outbound-webhook enable flag.
