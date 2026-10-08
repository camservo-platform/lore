#!/usr/bin/env bash
# Repeatable deploy/ops wrapper for the Lore backing services.
#
#   ./deploy.sh [deploy]        install or upgrade (idempotent)
#   ./deploy.sh diff            render manifests and diff against the cluster
#   ./deploy.sh status          show pods, services, PVCs
#   ./deploy.sh set-key <name>  store a hosted-service key (deepgram|llm), read from stdin
#   ./deploy.sh secret <key>    print one key from the credentials secret
#   ./deploy.sh psql [args]     psql into the lore database as the app user
#   ./deploy.sh qdrant <path>   curl the Qdrant API with the api key (e.g. /collections)
#   ./deploy.sh embed <text>    smoke-test the embedding model
#   ./deploy.sh forward         port-forward Postgres :5432, Qdrant :6333, embeddings :11434
#   ./deploy.sh logs [comp]     tail logs (postgres|qdrant|embeddings)
#   ./deploy.sh uninstall       remove the release (PVCs and credentials are kept)
#
# Overrides: NAMESPACE, RELEASE, KUBE_CONTEXT, VALUES (default: values.yaml)
# Keys picked up on deploy if set: DEEPGRAM_API_KEY, LLM_API_KEY
set -euo pipefail

cd "$(dirname "$0")"

NAMESPACE="${NAMESPACE:-lore}"
RELEASE="${RELEASE:-lore}"
VALUES="${VALUES:-values.yaml}"
KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context)}"
SECRET="$(awk '/^credentialsSecret:/ {print $2}' "$VALUES")"
SECRET="${SECRET:-lore-credentials}"

kc()   { kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" "$@"; }
hm()   { helm --kube-context "$KUBE_CONTEXT" -n "$NAMESPACE" "$@"; }
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

ensure_namespace() {
  kubectl --context "$KUBE_CONTEXT" create namespace "$NAMESPACE" \
    --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
}

ensure_credentials() {
  if kc get secret "$SECRET" >/dev/null 2>&1; then
    log "Credentials secret '$SECRET' already exists"
  else
    log "Creating credentials secret '$SECRET'"
    kc create secret generic "$SECRET" \
      --from-literal=postgres-password="$(rand 32)" \
      --from-literal=qdrant-api-key="$(rand 40)"
  fi
}

# Sets one key in the credentials secret without touching the others.
put_secret_key() {
  local key="$1" value="$2" b64
  b64="$(printf '%s' "$value" | base64 | tr -d '\n')"
  kc patch secret "$SECRET" --type merge -p "{\"data\":{\"$key\":\"$b64\"}}" >/dev/null
}

has_secret_key() {
  [[ -n "$(kc get secret "$SECRET" -o jsonpath="{.data.$1}" 2>/dev/null)" ]]
}

ensure_api_keys() {
  local name key env
  for name in deepgram llm; do
    key="$name-api-key"
    env="$(tr '[:lower:]' '[:upper:]' <<<"$name")_API_KEY"
    if [[ -n "${!env:-}" ]]; then
      log "Storing $key from \$$env"
      put_secret_key "$key" "${!env}"
    elif ! has_secret_key "$key"; then
      warn "No $key yet: run '$0 set-key $name' (or set \$$env and redeploy)"
    fi
  done
}

cmd_deploy() {
  log "Context: $KUBE_CONTEXT  Namespace: $NAMESPACE  Release: $RELEASE"
  helm lint ./chart -f "$VALUES" >/dev/null
  ensure_namespace
  ensure_credentials
  ensure_api_keys
  log "helm upgrade --install"
  hm upgrade --install "$RELEASE" ./chart -f "$VALUES" --timeout 10m
  log "Waiting for rollouts..."
  kc rollout status statefulset/"$RELEASE"-postgres --timeout=5m
  kc rollout status statefulset/"$RELEASE"-qdrant --timeout=5m
  if kc get statefulset "$RELEASE"-embeddings >/dev/null 2>&1; then
    # First start downloads the model in the init container.
    kc rollout status statefulset/"$RELEASE"-embeddings --timeout=10m
  fi
  cmd_status
}

cmd_diff() {
  hm template "$RELEASE" ./chart -f "$VALUES" | kc diff -f - || true
}

cmd_status() {
  kc get statefulset,pod,svc,pvc
  local key
  for key in deepgram-api-key llm-api-key; do
    has_secret_key "$key" && echo "$key: set" || echo "$key: MISSING"
  done
}

cmd_set_key() {
  local name="${1:-}" value
  [[ "$name" == deepgram || "$name" == llm ]] || { echo "usage: $0 set-key deepgram|llm" >&2; exit 1; }
  ensure_namespace
  ensure_credentials
  if [[ -t 0 ]]; then
    read -rsp "$name API key: " value; echo
  else
    IFS= read -r value
  fi
  [[ -n "$value" ]] || { echo "empty key, nothing stored" >&2; exit 1; }
  put_secret_key "$name-api-key" "$value"
  log "Stored $name-api-key in '$SECRET' (restart app pods to pick it up)"
}

cmd_secret() {
  kc get secret "$SECRET" -o jsonpath="{.data.${1:?usage: $0 secret <key>}}" | base64 -d; echo
}

cmd_psql() {
  kc exec -it "$RELEASE"-postgres-0 -c postgres -- \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' psql "$@"
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

cmd_qdrant() {
  local path="${1:-/collections}" key; shift || true
  key="$(cmd_secret qdrant-api-key)"
  _qdrant() {
    local base="$1"; shift
    curl -sS -H "api-key: $key" -H 'Content-Type: application/json' "$@" "$base$path"; echo
  }
  with_forward qdrant 6333 _qdrant "$@"
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
  log "Postgres localhost:5432  Qdrant localhost:6333  Embeddings localhost:11434 (Ctrl-C to stop)"
  kc port-forward svc/"$RELEASE"-postgres 5432:5432 &
  kc port-forward svc/"$RELEASE"-qdrant 6333:6333 &
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
  log "Release removed. PVCs and secret '$SECRET' were kept."
  log "To wipe everything: kubectl -n $NAMESPACE delete pvc,secret --all"
}

case "${1:-deploy}" in
  deploy|install|upgrade) cmd_deploy ;;
  diff)      cmd_diff ;;
  status)    cmd_status ;;
  set-key)   cmd_set_key "${2:-}" ;;
  secret)    cmd_secret "${2:-}" ;;
  psql)      shift; cmd_psql "$@" ;;
  qdrant)    shift; cmd_qdrant "$@" ;;
  embed)     shift; cmd_embed "$@" ;;
  forward)   cmd_forward ;;
  logs)      cmd_logs "${2:-}" ;;
  uninstall) cmd_uninstall ;;
  *) sed -n '2,17p' "$0"; exit 1 ;;
esac
