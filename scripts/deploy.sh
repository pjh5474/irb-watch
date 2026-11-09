#!/usr/bin/env bash
# Runs ON THE SERVER, started by the GitHub Actions deploy through a locked SSH key.
# The key's authorized_keys line forces this script (see docs/deploy-ec2.md section 9), so the
# only thing the workflow can do is deploy one commit. The workflow passes the commit hash as
# the "command"; sshd hands it to us in SSH_ORIGINAL_COMMAND.
#
# Everything is inside main() and the script ends with `exit`, so bash never reads a changed
# file from disk halfway through (the script itself is replaced by the git merge below).
main() {
  set -euo pipefail
  cd "$(dirname "$0")/.."

  sha="${SSH_ORIGINAL_COMMAND:-}"
  if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "deploy: expected a 40-character commit hash, got: '${sha:0:60}'" >&2
    return 2
  fi

  echo "deploy: fetching"
  git fetch --quiet origin main
  # Only commits that are on origin/main and move the server forward (never rewinds).
  if ! git merge-base --is-ancestor "$sha" origin/main; then
    echo "deploy: $sha is not on origin/main" >&2
    return 3
  fi
  git merge --ff-only "$sha"

  echo "deploy: building and starting"
  docker compose up -d --build

  echo "deploy: waiting for the API to be healthy (up to 180 s)"
  cid="$(docker compose ps -q api)"
  for _ in $(seq 1 36); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
    if [[ "$status" == "healthy" ]]; then
      echo "deploy: healthy at $(git rev-parse --short HEAD)"
      docker image prune -f >/dev/null
      return 0
    fi
    sleep 5
  done
  echo "deploy: API not healthy after 180 s (status: $status). Last logs:" >&2
  docker compose logs --tail 40 api >&2
  return 1
}

main "$@"
exit $?
