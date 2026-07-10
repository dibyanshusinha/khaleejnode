"""KhaleejNode extraction accuracy & degradation benchmark harness.

Submodules:
    degrade   -- programmatic document damage at graded severity levels
    fixtures  -- ground-truth manifests + clean renderer
    evaluate  -- field-level accuracy scoring (overall + critical fields)
    run_benchmark -- CLI orchestrator producing the accuracy-vs-degradation curve
"""

__all__ = ["degrade", "fixtures", "evaluate", "run_benchmark"]
