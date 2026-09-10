# FraudShield Autonomous Response — Phase 2

## Demo flow
1. Sign in to the lender/institution workspace and switch to FraudShield.
2. Open **Fraud Autopilot** under Operations.
3. Keep the default action ladder: Allow <40, Monitor 40+, Step-up 70+, Hold + Case 85+, Critical Hold + SOC 95+.
4. Click **Scan Current Alerts**. Existing high-risk demo transactions are evaluated idempotently and qualifying cases are opened automatically.
5. Open the response timeline. Download an evidence bundle for a high-risk event.
6. Open the corresponding transaction to show the autonomous response state alongside SHAP/rule/model evidence.
7. Use **Open official portal** only after analyst review. FraudShield intentionally does not auto-file a government complaint from a model score.

## Live-event behavior
`POST /lender/fraudshield/intelligence/ingest` now applies the response policy immediately after scoring a newly inserted transaction. The payment webhook does the same. The API response includes `autopilot.action`, `case_number`, and notification status.

## Internal SOC notification
For a real internal security webhook, configure:

- `FRAUDSHIELD_SOC_WEBHOOK_URL=https://your-internal-soc.example/...`
- `FRAUDSHIELD_ALLOW_OUTBOUND_WEBHOOKS=1`

Without these variables the project safely records critical notification state as `QUEUED_DEMO`, which is useful for a hackathon demonstration without sending data externally.

## Database
Run `migrations/002_fraud_autopilot.sql` if your deployment account does not allow the app to create tables at runtime.
