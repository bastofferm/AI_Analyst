"""Agentic alpha-model research — an iterative, critiqued replacement for one-shot training.

``api.quant.qlib_alpha.train`` fits one model on one fixed parametrization and promotes it
unconditionally. This package instead runs a LangGraph loop in which four agents — a
Quantitative Researcher, a Model Validation unit, a Portfolio Manager and an External
Advisor — iterate over a :class:`~api.quant.research.spec.TrainingSpec`, with every round
producing a model validation report that ends in an auditable robustness rating.

Layout
------
``spec``        the search space the agents mutate, and the validated patch applier
``preprocess``  sample selection, winsorization, normalization, imputation, purged splits
``models``      multi-family fitting behind one ``.model.predict(ndarray)`` contract
``evaluate``    purged walk-forward OOS scoring + the quality-attribute metric battery
``perturb``     the perturbation battery and the ordinal robustness rating
``report``      the iteration report, and the leakage-safe packet the agents are shown
``schemas``     Pydantic structured outputs for the four agents
``prompts``     the four personas, written as FlowMind-style lectures
``nodes``       LangGraph node functions
``graph``       graph assembly, ``run_research`` and the CLI
``runner``      the background job + Postgres ledger the REST API drives

Design boundaries that the rest of the package depends on:

* **The LLM never runs code and never sees the panel.** Agents return validated Pydantic
  objects; :func:`spec.apply_patch` whitelists and clamps every field before it can reach a
  fit. (FlowMind, arXiv:2404.13050.)
* **The LLM never sees ticker-level data.** Agent packets carry aggregate metrics and bucket
  statistics only, so memorized market knowledge cannot leak into the search.
  (Papasotiriou et al., arXiv:2411.00856.)
"""
