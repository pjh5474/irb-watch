#!/usr/bin/env bash
# Writes the server's .env from AWS Systems Manager Parameter Store, so settings can be changed
# in the AWS console and nobody has to SSH in to edit a file. Runs ON THE SERVER, started by
# scripts/deploy.sh. It needs the AWS CLI and an instance role that may read the parameter path
# (docs/deploy-ec2.md section 11).
#
#   SSM_PATH    the parameter path, for example /irb-watch/prod  (or pass it as the 1st argument)
#   ENV_FILE    where to write (default: .env in the repository root)
#   AWS_REGION  default ap-northeast-2
#
# Each parameter directly under the path becomes one KEY='value' line (the parameter name is the
# key, for example /irb-watch/prod/OPENAI_API_KEY -> OPENAI_API_KEY). The file is written to a
# temporary file with mode 600 and moved into place only when everything is valid, so a failure
# leaves the old .env untouched. Values are never printed or logged; errors name only the KEY.
set -euo pipefail
umask 077

path="${SSM_PATH:-${1:-}}"
target="${ENV_FILE:-$(dirname "$0")/../.env}"
region="${AWS_REGION:-ap-northeast-2}"

if [[ -z "$path" ]]; then
  echo "load-env: give the parameter path (SSM_PATH or the first argument), e.g. /irb-watch/prod" >&2
  exit 2
fi
if [[ ! "$path" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
  echo "load-env: the parameter path may use only letters, digits and _ . / -" >&2
  exit 2
fi
path="${path%/}"

# The AWS CLI installed with snap lives in /snap/bin, which is not in the PATH of the limited SSH
# session that runs deploy.sh.
export PATH="$PATH:/snap/bin:/usr/local/bin"
if ! command -v aws >/dev/null 2>&1; then
  echo "load-env: the AWS CLI was not found (install it: sudo snap install aws-cli --classic)" >&2
  exit 4
fi

tmp="$(mktemp "${target}.XXXXXX")"
trap 'rm -f "$tmp" "$tmp.json"' EXIT

if ! aws ssm get-parameters-by-path --region "$region" --path "$path" --with-decryption \
    --output json > "$tmp.json"; then
  echo "load-env: could not read the parameters (instance role? path? region?)" >&2
  exit 3
fi

"${PYTHON:-python3}" - "$path" "$tmp.json" "$tmp" <<'PY'
import json
import re
import sys

path, source, dest = sys.argv[1:4]
parameters = json.load(open(source)).get("Parameters", [])
lines = []
for item in parameters:
    key = item["Name"][len(path):].lstrip("/")
    if "/" in key:
        continue  # only parameters directly under the path
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        sys.exit(f"load-env: '{key}' is not a valid setting name (use CAPITALS, digits and _)")
    value = item["Value"]
    if any(ch in value for ch in ("\n", "\r", "\0", "'")):
        sys.exit(f"load-env: the value of {key} has a newline or a single quote, which .env cannot hold")
    lines.append(f"{key}='{value}'")
if not lines:
    sys.exit(f"load-env: no parameters found under {path}")
with open(dest, "w") as out:
    out.write("\n".join(sorted(lines)) + "\n")
print(f"load-env: wrote {len(lines)} settings")
PY

chmod 600 "$tmp"
mv -f "$tmp" "$target"
