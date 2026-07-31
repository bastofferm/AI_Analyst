# Screenshots

Reference captures of the AI_Analyst frontend (Next.js, `frontend/`), one per
view, taken against live US warehouse data at 1440×1024 (2× DPI).

| File | View | What it shows |
|---|---|---|
| `01-home.png` | Home | Landing hero + live "market pulse" sector strip + how-it-works |
| `02-explore.png` | Explore | Coverage-universe browser with per-company brand logos |
| `03-analyze-msft.png` | Analyze | A single name worked up by the committee — MSFT (Microsoft) |
| `04-compare.png` | Compare | Relative-value sector ranking setup |
| `05-ideas.png` | Ideas | One-click quick scan + natural-language screen |
| `06-quant.png` | Quant | qlib return / risk / portfolio desk with alpha-model predictions |

Regenerate after UI changes with the puppeteer script kept in the scratchpad
(`shots.js`) while the backend (`:8027`) and frontend (`:3027`) are running —
it drives the six views and re-saves these files.
