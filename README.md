# Roofing Missed-Call Recovery MVP

A simple FastAPI MVP for simulating missed-call SMS follow-up and qualifying roofing leads.

## Features
- Create a missed-call lead
- Auto-send first SMS
- Simulate inbound SMS replies
- Qualify lead through:
  - job type
  - postal code
  - urgency
- View conversation history
- View lead dashboard

## Setup

### 1. Create and activate a virtual environment

#### Mac / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
```

## Stripe mode configuration

Checkout mode is controlled by the API keys and price IDs you provide via environment variables.

- `STRIPE_MODE=live` (default) will first look for `STRIPE_LIVE_*` variables.
- `STRIPE_MODE=test` will first look for `STRIPE_TEST_*` variables.
- If mode-specific variables are missing, the app falls back to legacy `STRIPE_*` variables.

Supported mode-specific variables:
- `STRIPE_<MODE>_SECRET_KEY`
- `STRIPE_<MODE>_WEBHOOK_SECRET`
- `STRIPE_<MODE>_PRICE_PILOT`
- `STRIPE_<MODE>_PRICE_PILOT_SETUP`
- `STRIPE_<MODE>_PRICE_GROWTH`
- `STRIPE_<MODE>_PRICE_GROWTH_SETUP`

Example for live mode:

```bash
STRIPE_MODE=live
STRIPE_LIVE_SECRET_KEY=sk_live_...
STRIPE_LIVE_WEBHOOK_SECRET=whsec_...
STRIPE_LIVE_PRICE_PILOT=price_...
STRIPE_LIVE_PRICE_PILOT_SETUP=price_...
STRIPE_LIVE_PRICE_GROWTH=price_...
STRIPE_LIVE_PRICE_GROWTH_SETUP=price_...
```
