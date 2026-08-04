# Deprecated documentation sources

These Markdown files were the pre-LaTeX sources for three of the reference PDFs.
As of the July 2026 rewrite, the reference documents are authored and maintained as
**pdfLaTeX** in `docs/*.tex` (sharing `docs/mzqastyle.sty`), so these Markdown files
are frozen and no longer maintained — they are kept only for history and will drift
from the current PDFs.

| Deprecated file | Superseded by |
|---|---|
| `ai_analyst_documentation.md` | `../AI_Analyst_Documentation.tex` → `.pdf` |
| `ai_analyst_modeling_reference.md` | `../ai_analyst_modeling_reference.tex` → `.pdf` |
| `qlib_integration.md` | `../qlib_integration.tex` → `.pdf` |

Do not edit these; edit the `.tex` sources and rebuild with `latexmk -pdf`.
