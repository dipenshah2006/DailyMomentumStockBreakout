---
name: Python post-merge setup
description: The project’s post-merge dependency setup runs inside Replit’s Nix-managed Python environment.
---

The post-merge dependency install must use pip’s non-interactive externally-managed-environment override.

**Why:** Replit’s Nix Python rejects ordinary system-level pip installs under PEP 668, causing the automatic merge setup to fail even when dependencies are already available.

**How to apply:** Keep the setup idempotent and use `python -m pip install --no-input --break-system-packages -r requirements.txt` when installing the project requirements.