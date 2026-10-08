#!/usr/bin/env bash
# Repeatable deploy/ops wrapper for Lore on Kubernetes.
#
#   ./deploy.sh [deploy]          install or upgrade (idempotent)
#   ./deploy.sh diff              render manifests and diff against the cluster
#   ./deploy.sh status            show workloads, PVCs, keys and users
#   ./deploy.sh set-key <name>    store a hosted-service key (deepgram|llm), read from stdin
#   ./deploy.sh secret <key>      print one key from the credentials secret
#   ./deploy.sh add-user <name>   create/reset an ingress login; prints the generated password
#   ./deploy.sh remove-user <name>
#   ./deploy.sh users             list ingress logins
#   ./deploy.sh psql [args]       psql into the lore database as the app user
#   ./deploy.sh redis [args]      redis-cli (e.g. ./deploy.sh redis keys 'lore:*')
#   ./deploy.sh embed <text>      smoke-test the embedding model
#   ./deploy.sh forward           port-forward Postgres, Redis, embeddings and MCP servers
#   ./deploy.sh logs [comp]       tail logs (postgres|redis|embeddings|mcp-game|mcp-lore)
#   ./deploy.sh uninstall         remove the release (PVCs, credentials and users are kept)
#
# Local, gitignored files picked up when present:
#   secrets.env         DEEPGRAM_API_KEY / LLM_API_KEY, stored in the cluster on deploy
#   values.local.yaml   site settings layered over values.yaml (ingress host, annotations)
#
# Overrides: NAMESPACE, RELEASE, KUBE_CONTEXT, APP_TAG (default: last commit touching app/)
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f secrets.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source secrets.env
  set +a
fi

NAMESPACE="${NAMESPACE:-lore}"
RELEASE="${RELEASE:-lore}"
KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context)}"
SECRET="$(awk '/^credentialsSecret:/ {print $2}' values.yaml)"
USERS_SECRET="$(awk '/^usersSecret:/ {print $2}' values.yaml)"
VALUES_ARGS=(-f values.yaml)
[[ -f values.local.yaml ]] && VALUES_ARGS+=(-f values.local.yaml)

kc()   { kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" "$@"; }
hm()   { helm --kube-context "$KUBE_CONTEXT" -n "$NAMESPACE" "$@"; }
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
# `|| true`: tr dies of SIGPIPE when head has enough, which pipefail would report as failure.
rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1" || true; }

# Images are tagged by commit; deploy the newest commit that changed the app.
app_tag() {
  if [[ -n "${APP_TAG:-}" ]]; then echo "$APP_TAG"; return; fi
  if [[ -n "$(git status --porcelain -- ../app)" ]]; then
    warn "app/ has uncommitted changes; they are not in any image" >&2
  fi
  git log -1 --format=%H -- ../app
}

helm_args() {
  echo "${VALUES_ARGS[@]}" --set "app.tag=$(app_tag)"
}

ensure_namespace() {
  kubectl --context "$KUBE_CONTEXT" create namespace "$NAMESPACE" \
    --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
}

# Sets one key in a secret without touching the others.
put_secret_key() {
  local secret="$1" key="$2" value="$3" b64
  b64="$(printf '%s' "$value" | base64 | tr -d '\n')"
  kc patch secret "$secret" --type merge -p "{\"data\":{\"$key\":\"$b64\"}}" >/dev/null
}

secret_value() {
  kc get secret "$1" -o jsonpath="{.data.${2//./\\.}}" 2>/dev/null | base64 -d
}

has_secret_key() {
  [[ -n "$(kc get secret "$SECRET" -o jsonpath="{.data.$1}" 2>/dev/null)" ]]
}

# Creates the credentials and users secrets if missing, and generates any missing
# generated key (so new components get a password on upgrade).
ensure_secrets() {
  local key
  kc get secret "$SECRET" >/dev/null 2>&1 || kc create secret generic "$SECRET" >/dev/null
  kc get secret "$USERS_SECRET" >/dev/null 2>&1 \
    || kc create secret generic "$USERS_SECRET" --from-literal=users="$(locked_user)" >/dev/null
  for key in postgres-password redis-password; do
    if ! has_secret_key "$key"; then
      log "Generating $key"
      put_secret_key "$SECRET" "$key" "$(rand 32)"
    fi
  done
}

ensure_api_keys() {
  local name key env
  for name in deepgram llm; do
    key="$name-api-key"
    env="$(tr '[:lower:]' '[:upper:]' <<<"$name")_API_KEY"
    if [[ -n "${!env:-}" ]]; then
      if [[ "$(secret_value "$SECRET" "$key")" != "${!env}" ]]; then
        log "Storing $key from \$$env"
        put_secret_key "$SECRET" "$key" "${!env}"
      fi
    elif ! has_secret_key "$key"; then
      warn "No $key yet: add $env to secrets.env (see secrets.env.example) and redeploy"
    fi
  done
}

cmd_deploy() {
  local tag
  tag="$(app_tag)"
  log "Context: $KUBE_CONTEXT  Namespace: $NAMESPACE  Release: $RELEASE  App: ${tag:0:12}"
  # shellcheck disable=SC2046
  helm lint ./chart $(helm_args) >/dev/null
  ensure_namespace
  ensure_secrets
  ensure_api_keys
  log "helm upgrade --install"
  # shellcheck disable=SC2046
  hm upgrade --install "$RELEASE" ./chart $(helm_args) --timeout 10m
  log "Waiting for rollouts..."
  kc rollout status statefulset/"$RELEASE"-postgres --timeout=5m
  kc rollout status statefulset/"$RELEASE"-redis --timeout=5m
  if kc get statefulset "$RELEASE"-embeddings >/dev/null 2>&1; then
    # First start downloads the model in the init container.
    kc rollout status statefulset/"$RELEASE"-embeddings --timeout=10m
  fi
  kc get deployment -l "app.kubernetes.io/instance=$RELEASE" -o name \
    | xargs -n1 -I{} kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" rollout status {} --timeout=5m
  cmd_status
}

cmd_diff() {
  # shellcheck disable=SC2046
  hm template "$RELEASE" ./chart $(helm_args) | kc diff -f - || true
}

cmd_status() {
  kc get statefulset,deployment,pod,svc,ingress,pvc
  local key
  for key in deepgram-api-key llm-api-key; do
    has_secret_key "$key" && echo "$key: set" || echo "$key: MISSING"
  done
  echo "ingress users: $(cmd_users | paste -sd ' ' -)"
}

cmd_set_key() {
  local name="${1:-}" value
  [[ "$name" == deepgram || "$name" == llm ]] || { echo "usage: $0 set-key deepgram|llm" >&2; exit 1; }
  ensure_namespace
  ensure_secrets
  if [[ -t 0 ]]; then
    read -rsp "$name API key: " value; echo
  else
    IFS= read -r value
  fi
  [[ -n "$value" ]] || { echo "empty key, nothing stored" >&2; exit 1; }
  put_secret_key "$SECRET" "$name-api-key" "$value"
  log "Stored $name-api-key in '$SECRET' (restart app pods to pick it up)"
}

cmd_secret() {
  secret_value "$SECRET" "${1:?usage: $0 secret <key>}"; echo
}

# Users live in the users secret as htpasswd lines (bcrypt), the format Traefik reads.
# Traefik rejects a basic-auth middleware with no users (dropping the routes, so 404s),
# so an empty list holds one placeholder whose password was never kept.
LOCKED_USER=_locked

locked_user() { htpasswd -nbB "$LOCKED_USER" "$(rand 32)"; }

users_file() { secret_value "$USERS_SECRET" users; }

write_users() {
  local users
  users="$(grep -v "^$LOCKED_USER:" <<<"$1" | sed '/^$/d' || true)"
  put_secret_key "$USERS_SECRET" users "${users:-$(locked_user)}"
}

cmd_users() {
  users_file | cut -d: -f1 | grep -v "^$LOCKED_USER$" | sed '/^$/d' || true
}

cmd_add_user() {
  local name="${1:?usage: $0 add-user <name>}" password line
  [[ "$name" =~ ^[a-z0-9_-]+$ ]] || { echo "usernames are lowercase letters, digits, _ and -" >&2; exit 1; }
  ensure_namespace
  ensure_secrets
  password="$(rand 24)"
  line="$(htpasswd -nbB "$name" "$password")"
  write_users "$( (users_file | grep -v "^$name:" ; echo "$line") | sed '/^$/d')"
  log "User '$name' saved. Password (shown once): $password"
}

cmd_remove_user() {
  local name="${1:?usage: $0 remove-user <name>}"
  write_users "$(users_file | grep -v "^$name:" || true)"
  log "User '$name' removed"
}

cmd_psql() {
  kc exec -it "$RELEASE"-postgres-0 -c postgres -- \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' psql "$@"
}

cmd_redis() {
  kc exec -it "$RELEASE"-redis-0 -c redis -- redis-cli "$@"
}

# Port-forwards <svc>:<port> to a random local port and runs `fn <base-url> [args...]`.
# (The images ship without curl, and the network policy blocks other namespaces.)
with_forward() {
  local svc="$1" port="$2" fn="$3" local_port pid rc=0; shift 3
  local_port=$((20000 + RANDOM % 20000))
  kc port-forward "svc/$RELEASE-$svc" "$local_port:$port" >/dev/null &
  pid=$!
  for _ in $(seq 50); do
    nc -z 127.0.0.1 "$local_port" 2>/dev/null && break
    sleep 0.1
  done
  "$fn" "http://127.0.0.1:$local_port" "$@" || rc=$?
  kill "$pid" 2>/dev/null || true
  return "$rc"
}

cmd_embed() {
  local text="${*:-the dragon sleeps beneath the mountain}" model
  model="$(kc get configmap "$RELEASE"-config -o jsonpath='{.data.EMBEDDINGS_MODEL}')"
  _embed() {
    curl -sS "$1/api/embed" -d "$(python3 -c 'import json,sys; print(json.dumps({"model":sys.argv[1],"input":sys.argv[2]}))' "$model" "$text")" \
      | python3 -c 'import json,sys; e=json.load(sys.stdin)["embeddings"][0]; print(f"{len(e)} dims, first 4: {e[:4]}")'
  }
  with_forward embeddings 11434 _embed
}

cmd_forward() {
  log "Postgres :5432  Redis :6379  Embeddings :11434  MCP game :8001/mcp/game  lore :8002/mcp/lore (Ctrl-C to stop)"
  kc port-forward svc/"$RELEASE"-postgres 5432:5432 &
  kc port-forward svc/"$RELEASE"-redis 6379:6379 &
  kc port-forward svc/"$RELEASE"-mcp-game 8001:8000 &
  kc port-forward svc/"$RELEASE"-mcp-lore 8002:8000 &
  if kc get svc "$RELEASE"-embeddings >/dev/null 2>&1; then
    kc port-forward svc/"$RELEASE"-embeddings 11434:11434 &
  fi
  trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
  wait
}

cmd_logs() {
  local comp="${1:-postgres}"
  kc logs -f -l "app.kubernetes.io/name=$comp,app.kubernetes.io/instance=$RELEASE" \
    --tail=100 --all-containers --prefix
}

cmd_uninstall() {
  hm uninstall "$RELEASE"
  log "Release removed. PVCs and secrets '$SECRET', '$USERS_SECRET' were kept."
  log "To wipe everything: kubectl -n $NAMESPACE delete pvc,secret --all"
}

case "${1:-deploy}" in
  deploy|install|upgrade) cmd_deploy ;;
  diff)        cmd_diff ;;
  status)      cmd_status ;;
  set-key)     cmd_set_key "${2:-}" ;;
  secret)      cmd_secret "${2:-}" ;;
  add-user)    cmd_add_user "${2:-}" ;;
  remove-user) cmd_remove_user "${2:-}" ;;
  users)       cmd_users ;;
  psql)        shift; cmd_psql "$@" ;;
  redis)       shift; cmd_redis "$@" ;;
  embed)       shift; cmd_embed "$@" ;;
  forward)     cmd_forward ;;
  logs)        cmd_logs "${2:-}" ;;
  uninstall)   cmd_uninstall ;;
  *) sed -n '2,24p' "$0"; exit 1 ;;
esac
