"""KhaleejNode secure core package (The Shield).

Submodules:
    config    -- constants, paths, secret material
    hardware  -- node-lock hardware fingerprint
    license   -- machine-bound license.key generation & verification
    vault     -- HMAC row-signed prepaid credit ledger
    refill    -- offline public-key credit token redemption
"""

__all__ = ["config", "hardware", "license", "vault", "refill"]
