# Foxio Video Agent

Automated browser workflow capture + AI-powered tutorial video generation.

This monorepo contains two LangGraph-based agents that work together as a pipeline:

1. **Agent 1 (Playwright)** — Navigates a web application, records the screen, captures DOM context, and produces structured workflow metadata.
2. **Agent 2 (Video Generation)** — Takes the raw recording from Agent 1, overlays a polished mouse cursor with click animations using Grok Imagine Video, and outputs a tutorial-ready video.

---

## Architecture

```
┌─────────────────────┐       raw_video.mp4        ┌─────────────────────┐
│  agent1_playwright   │  ───────────────────────►  │   agent2_video_gen   │
│                      │  cursor_actions.json       │                      │
│  • Playwright browser│                            │  • Grok Imagine Video│
│  • Azure OpenAI      │                            │  • FFmpeg split/merge│
│  • LangGraph workflow│                            │  • LangGraph pipeline│
└─────────────────────┘                            └─────────────────────┘
```

---

## How It Works

### Agent 1 — Browser Capture & Analysis

```
User asks: "How do I create a new contract?"
         ↓
1. NAVIGATE & RECORD — Opens browser, handles login, records screen
2. CAPTURE PAGES    — DOM extraction, screenshots, cursor tracking (1-6 pages)
3. LLM ANALYSIS     — GPT-4o generates step-by-step guidance + video narration script
4. OUTPUT           — raw_video.mp4 + cursor_actions.json + Agent1Output (JSON)
```

Key behavior: Focused, not exploratory. Only captures pages relevant to the user's task.

### Agent 2 — Video Enhancement

```
Agent 1 Output → Step Splitter → Generate Clips (xAI) → Concatenate (FFmpeg) → tutorial_video.mp4
```

Splits the narration into 4-8 clips, generates video with realistic cursor animations, and merges into a final tutorial.

---

## Prerequisites

- Python 3.10+
- FFmpeg (must be in PATH)
- Node.js (for Playwright browser binaries)
- Azure OpenAI access (Agent 1)
- xAI API key (Agent 2)

---

## Installation

```bash
git clone <repo-url>
cd Video_Agent

python -m venv .venv
.venv\Scripts\activate     # Windows
source .venv/bin/activate  # Linux/macOS

pip install -e .
playwright install chromium
```

---

## Configuration

```bash
cp .env.example .env
```

### Key Environment Variables

| Variable | Agent | Description |
|----------|-------|-------------|
| `AZURE_OPENAI_ENDPOINT` | 1 | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_API_KEY` | 1 | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | 1 | Model deployment name (default: `gpt-4o`) |
| `XAI_API_KEY` | 2 | xAI API key for Grok Imagine Video |
| `BROWSER_USE_HEADLESS` | 1 | `true` for server deployment |
| `URL_ALLOWLIST` | 1 | Comma-separated approved domains |
| `MAX_COST_PER_SESSION_USD` | 1 | Budget cap per session (default: `1.0`) |
| `DRY_RUN` | 2 | Skip xAI API calls (default: `true`) |

See `.env.example` for all options.

---

## Usage

### Full Pipeline (Agent 1 → Agent 2)

```bash
python run_pipeline.py --url "https://your-app.com" --query "How do I create a new contact?"
```

### Agent 1 Only (browser capture)

```python
import asyncio
from langgraph_browser_agent import run_agent1

result = asyncio.run(run_agent1(
    url="https://your-app.com",
    user_query="How do I create a new contact?",
))
```

### Agent 2 Only (video enhancement)

```bash
cd agent2_video_gen
python main.py --input ../agent1_playwright/.data/video_clips/raw_long_video.mp4
```

---

## Folder Structure

```
Video_Agent/
├── agent1_playwright/
│   ├── src/langgraph_browser_agent/
│   │   ├── agent.py               # run_agent1() entry point
│   │   ├── graph.py               # LangGraph workflow definition
│   │   ├── nodes.py               # Navigate + verify + analyze nodes
│   │   ├── navigation_planner.py  # LLM-driven navigation planning
│   │   ├── browser_pool.py        # Browser context + video recording
│   │   ├── browser_helpers.py     # DOM extraction, popup dismissal
│   │   ├── cursor_recorder.py     # Mouse/keyboard event capture
│   │   ├── video_merger.py        # FFmpeg merge/filter pipeline
│   │   ├── pipeline.py            # Full pipeline orchestration
│   │   ├── llm.py                 # Azure OpenAI client
│   │   ├── cache.py               # Response caching
│   │   ├── cost_tracker.py        # Per-call cost tracking
│   │   ├── config.py              # Centralized configuration
│   │   ├── security.py            # URL validation & blocklist
│   │   ├── models.py              # Pydantic schemas
│   │   ├── prompts.py             # LLM system prompt
│   │   ├── state.py               # AgentState TypedDict
│   │   └── logger.py              # Structured logging + audit
│   ├── .data/                      # Runtime data (gitignored)
│   │   ├── video_clips/            # Raw recordings + merged output
│   │   ├── screenshots/            # Page screenshots
│   │   ├── browser_data/           # Persistent browser state
│   │   ├── cache/                  # LLM response cache
│   │   └── logs/                   # Audit + cost logs
│   └── requirements.txt
│
├── agent2_video_gen/
│   ├── generate_tutorial.py        # Main entry point
│   ├── main.py                     # CLI runner
│   ├── adapters/
│   │   └── grok_adapter.py         # xAI Grok Imagine Video client
│   ├── graph/
│   │   └── workflow.py             # LangGraph pipeline (4-node DAG)
│   ├── nodes/
│   │   ├── step_splitter.py        # Narration → structured clips
│   │   ├── video_generator.py      # Clip generation with retry
│   │   └── utils.py                # FFmpeg concat utilities
│   ├── config/
│   │   └── settings.py             # Pydantic settings
│   ├── output/generated_videos/    # Final tutorial videos
│   └── requirements.txt
│
├── run_pipeline.py                 # Unified entry point
├── .env.example                    # Environment variable template
├── requirements.txt                # Combined dependencies
├── pyproject.toml                  # Package configuration
└── .gitignore
```

---

## Production Deployment

### Security
- URL allowlist/blocklist enforced before any navigation
- All inputs validated, no raw user strings in browser commands
- Session cookies stored locally, never committed to git

### Reliability
- Retry with exponential backoff on browser + LLM operations
- Browser pool with graceful cleanup on crash/timeout
- Budget enforcement prevents runaway API costs

### Performance
- Focused capture (1-6 pages) instead of full-site crawl
- LLM response caching for repeated queries
- Tiered models (GPT-4o-mini for simple tasks)
- DOM filtering strips noise before LLM (~40% fewer tokens)

### Observability
- Structured logging with configurable levels
- JSON audit trail of every agent action
- Per-session cost tracking with budget alerts
- Optional LangSmith tracing (`LANGCHAIN_TRACING_V2=true`)

---

## Development

```bash
pytest
ruff check .
ruff format .
```

---

## License

MIT
