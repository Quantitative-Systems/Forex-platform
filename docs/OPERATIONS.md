# Operations

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

## Control plane

```bash
cp .env.example .env
forex-platform serve-production
```

The default environment is `PAPER`. Do not enable live trading until the qualification, demo, and human-approval requirements are satisfied.

Required operational properties:

- `FOREX_API_KEY_PEPPER` is random and at least 32 characters.
- `.env` and secret files are never committed.
- Broker configuration is stored outside the repository.
- TLS is used for public or live deployments.
- `/readyz` is used as the deployment readiness check.
- `/healthz` is used as the liveness check.

## Docker

```bash
cp .env.example .env
mkdir -p secrets
# Mount broker config and secret files as described in docker-compose.yml.
docker compose up -d --build
```

Inspect:

```bash
docker compose logs -f control-plane
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/readyz
```

## systemd

The unit file is:

```text
deploy/forex-platform.service
```

Install it only after creating the `forex` user, directories, environment file, broker configuration, and secrets with restrictive permissions.

## Windows MT5 gateway

Install the official MetaTrader5 package and configure:

```text
MT5_AGENT_TOKEN
MT5_AGENT_HMAC_SECRET
MT5_AGENT_ACCOUNT_ID
```

Run:

```bash
python tools/mt5_agent.py \
  --host 0.0.0.0 \
  --port 8788 \
  --tls-cert cert.pem \
  --tls-key key.pem
```

The control plane must reach the agent over HTTPS. The agent rejects:

- Invalid bearer tokens.
- Invalid HMAC signatures.
- Replayed nonces.
- Requests for a different account.
- Missing or unavailable MetaTrader5.
- Failed MT5 order operations.

## Data operations

Download all conventional spot pairs:

```bash
forex-platform download-history \
  --all-fx \
  --timeframes M15 \
  --years 5 \
  --cache-dir data/cache \
  --max-concurrent 3
```

This writes a manifest such as:

```text
data/cache/manifest_M15.json
```

The manifest records provenance, row counts, date ranges, and quality results. Batch downloads fail closed if real data cannot be obtained. Synthetic fallback is explicit and non-qualifying.

## Routine checks

```bash
forex-platform status
curl http://127.0.0.1:8787/metrics
curl http://127.0.0.1:8787/readyz
```

Review:

- Broker connectivity and all-enabled-account readiness.
- Reconciliation alerts.
- Daily loss and drawdown circuit breakers.
- News blackout status.
- Data freshness and quality.
- Audit records for privileged actions.

## Emergency procedures

1. Arm the global kill switch from the operator dashboard/API.
2. Verify the affected broker/pair is blocked.
3. Inspect broker positions directly.
4. Use `flatten_all` only with explicit operator authorization.
5. Preserve audit logs and broker screenshots/tickets.
6. Do not re-enable live routing until the cause is understood and reconciled.

## Security notes

- Never commit `.env`, API keys, HMAC secrets, broker credentials, certificates, or private keys.
- The Windows agent is the only component that should access MetaTrader5 credentials.
- Treat broker and market-data provider terms as part of the production design.
- Run legal, compliance, risk, and operational review before handling client or live capital.
