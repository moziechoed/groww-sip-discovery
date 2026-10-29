# Groww SIP Continuation Discovery Engine

AI-powered qualitative research engine for identifying why first-time SIP investors on Groww pause, reduce, or stop their SIPs within the first 12 months.

## Quickstart (P0)

1. Copy `.env.example` to `.env` and add your `GEMINI_API_KEY`.
2. Activate the Python 3.12 virtual environment:
   ```bash
   source .venv/bin/activate
   ```
3. Run offline unit tests (uses fake LLM provider; zero API calls):
   ```bash
   pytest
   ```
4. Run P0 preflight (`models.list` metadata check):
   ```bash
   python run.py --stage preflight
   ```
5. Run the 1-post smoke test call (requires typing `yes` at the approval gate):
   ```bash
   python run.py --stage test-call
   ```

## Public demo (P10)

- App: `app/streamlit_app.py` (Streamlit Community Cloud, main file path `app/streamlit_app.py`).
- The app reads only `outputs/` and `data/seeds/`. Raw, clean and enriched post text is never committed (see `.gitignore`).
- Live Analysis needs `GEMINI_API_KEY` in the app's Streamlit Secrets (never in this repo). Limits: 3 per session, 50 per day, 2,000 characters.
- Pipeline: `python run.py --stage collect | clean | golden-sample | enrich | analyse | surface | evaluate`. Every Gemini-calling stage prints an estimate and waits for a typed `yes`.

Research tool built on public data. Not investment advice.
