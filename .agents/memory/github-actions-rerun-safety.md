---
name: GitHub Actions rerun safety
description: Durable workflow constraints for generated reports and GitHub Pages deployments.
---

Generated report jobs that modify tracked files must use `git pull --rebase --autostash` before rebasing, or the pull fails because the generated file is dirty. GitHub Pages artifacts should include the run ID and attempt in their name, with the same value passed to `deploy-pages`, because reruns retain the earlier default `github-pages` artifact and deployment becomes ambiguous.

**Why:** A rerun of the scheduled report workflow failed once from rebasing with a dirty generated HTML file and again because two retained Pages artifacts shared the default name.

**How to apply:** Use a run/attempt-specific Pages artifact name in every workflow that can be rerun, and autostash generated tracked files before pulling or rebasing.