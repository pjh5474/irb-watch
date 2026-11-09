#!/usr/bin/env bash
# Runs ON THE SERVER, started by GitHub Actions through a locked SSH key.
# The key's authorized_keys line forces this script (see docs/deploy-ec2.md section 9), so the
# only things a workflow can do are the two commands below. The workflow passes the command as
# the SSH "command"; sshd hands it to us in SSH_ORIGINAL_COMMAND:
#   <40-character commit hash>  deploy that commit: pull it, load settings, rebuild, restart
#   refresh                     load settings again and restart (no code change)
#
# Settings: if the file .ssm-path exists in the repository (first line = a Parameter Store path
# such as /irb-watch/prod, created by the owner, never committed), .env is rewritten from AWS
# Systems Manager Parameter Store first (scripts/load-env.sh). Without that file .env is left
# as it is, so nothing changes until the owner switches over.
#
# Everything is inside main() and the script ends with `exit`, so bash never reads a changed
# file from disk halfway through (the script itself is replaced by the git merge below).
main() {
  set -euo pipefail
  cd "$(dirname "$0")/.."

  command="${SSH_ORIGINAL_COMMAND:-}"
  if [[ "$command" == "refresh" ]]; then
    mode=refresh
  elif [[ "$command" =~ ^[0-9a-f]{40}$ ]]; then
    mode=deploy
  else
    echo "deploy: expected a 40-character commit hash or 'refresh', got: '${command:0:60}'" >&2
    return 2
  fi

  if [[ "$mode" == deploy ]]; then
    echo "deploy: fetching"
    git fetch --quiet origin main
    # Only commits that are on origin/main and move the server forward (never rewinds).
    if ! git merge-base --is-ancestor "$command" origin/main; then
      echo "deploy: $command is not on origin/main" >&2
      return 3
    fi
    git merge --ff-only "$command"
  fi

  if [[ -f .ssm-path ]]; then
    echo "deploy: loading settings from Parameter Store"
    SSM_PATH="$(head -n 1 .ssm-path | tr -d '\r\n ')" bash scripts/load-env.sh
  elif [[ "$mode" == refresh ]]; then
    echo "deploy: 'refresh' needs the file .ssm-path (see docs/deploy-ec2.md section 11)" >&2
    return 4
  fi

  if [[ "$mode" == deploy ]]; then
    echo "deploy: building and starting"
    docker compose up -d --build
  else
    echo "deploy: restarting with the new settings"
    docker compose up -d
  fi

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
