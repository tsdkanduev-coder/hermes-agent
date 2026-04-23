# Render staging

This fork is prepared for a first public Render deployment with Telegram in webhook mode.

## What this adds

- `scripts/render_gateway_proxy.py` binds Render's public port and exposes `/health`
- `scripts/render-gateway-start.sh` seeds first-run config and starts the wrapper
- `deploy/render-config.staging.yaml` narrows Telegram to a safer staging toolset
- `deploy/render-SOUL.md` sets the centralized concierge prompt
- `render.yaml` documents the expected Render service shape

## Runtime model

- Public Render port: `PORT` (default Render web-service port)
- Internal Hermes Telegram webhook port: `HERMES_INTERNAL_TELEGRAM_PORT` (default `8443`)
- Public webhook path: derived from `TELEGRAM_WEBHOOK_URL`

## Required environment variables

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_URL`
- `TELEGRAM_WEBHOOK_SECRET`
- one model-provider key such as `OPENROUTER_API_KEY`

## Access mode

For this staging setup, the committed `.env.render.example` uses:

`GATEWAY_ALLOW_ALL_USERS=true`

That is acceptable only because Telegram is narrowed to the `safe` + `todo`
toolsets in `deploy/render-config.staging.yaml`. Switch to
`TELEGRAM_ALLOWED_USERS=...` before enabling more powerful integrations.
