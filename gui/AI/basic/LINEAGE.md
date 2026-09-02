# Basic AI lineage

Production base: `ai_inference_POWERLINE_SAFE_NO_BLEED_FINAL (1).py`.

Reference versions reviewed:
- BASIC_HAG_FALLBACK_FINAL: robust HAG fallback / 5-class behavior.
- POWERLINE_CL_HARD_RESCUE_FINAL: CL-guided hard wire/pole recovery.
- POWERLINE_SAFE_NO_BLEED_FINAL: retains recovery while adding safer pruning / anti-bleed behavior.

CL is a Wire/Pole prior only. It must never restrict the normal 5-class Basic classifier.
