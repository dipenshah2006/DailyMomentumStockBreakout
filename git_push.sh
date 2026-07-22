#!/bin/bash
if [ -z "$GITHUB_TOKEN" ]; then
    echo "ERROR: GITHUB_TOKEN environment variable is not set."
    exit 1
fi

REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/dipenshah2006/DailyMomentumStockBreakout.git"
BRANCH="${1:-main}"
FORCE="${2:-}"

echo "Pushing to GitHub branch: $BRANCH"
if [ "$FORCE" = "--force" ]; then
    git push --force "$REPO_URL" HEAD:"$BRANCH"
else
    git push "$REPO_URL" HEAD:"$BRANCH"
fi
echo "Push complete."
