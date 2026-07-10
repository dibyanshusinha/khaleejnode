"""KhaleejNode compliance engine (offline).

Submodules:
    tariff      -- HS-code validation + duty calculation
    screening   -- denied-party / dual-use / country-risk screening
    crosschecks -- arithmetic reconciliation (catches silent misreads)
    engine      -- orchestrator: findings + scored compliance summary

All reference data under data/ is REPRESENTATIVE. Replace with licensed official
lists (GCC tariff, OFAC/UN/EU/UK/UAE sanctions) in the same schema for production.
"""

__all__ = ["tariff", "screening", "crosschecks", "engine"]
