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