#!/usr/bin/env bash
#
# Deploy one commit of this fork onto a server that already runs this stack,
# building the images on the server.
#
#   remote=$(ssh <host> 'mktemp -t agenticos-fork-deploy.XXXXXX')
#   ssh <host> "cat > $remote" < scripts/fork-deploy.sh
#   ssh <host> "trap 'rm -f $remote' EXIT; bash $remote <commit-or-tag>"
#
# The fork's variant of `scripts/deploy.sh`, which pulls the images upstream's CI
# publishes. This fork publishes none, so this builds `agenticos-fork-backend`
# and `agenticos-fork-frontend` from the commit it deploys and runs them through
# the `*.fork.yml` overlays. The rest is `deploy.sh`'s, for the reasons it gives
# there: copied and then run (#1488), a commit rather than a branch, the proxy
# and the sandbox read from `backend/.env`, one retry on a stale health status
# (#1831), and the health wait. When a sync changes `deploy.sh`, port the
# change here.
#
# The checkout's `origin` has to be this fork, or a fork tag does not resolve:
#
#   git remote set-url origin https://github.com/LovingCivilian/agenticos.git
#
# The images are built before anything stops, so a build that fails leaves the
# running stack as it was. The build takes CPU and memory on the host while it
# runs - the frontend's most of all.
#
# To roll back, run it again with the previous commit. The last two builds stay
# on the host, so that is a start, not a build. A migration that has run stays
# run, which is why a rollback across one is a decision rather than a command.
set -euo pipefail

REF="${1:?usage: fork-deploy.sh <commit-or-tag>}"
APP_DIR="${APP_DIR:-/opt/agenticos}"
COMPOSE_ENV="${APP_DIR}/backend/.env"
FRONTEND_PROJECT="agenticos-frontend"
# Builds of each image kept on the host: the one deployed and the one before it.
KEEP_BUILDS=2

say() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

cd "$APP_DIR"
test -f "$COMPOSE_ENV" || { echo "missing $COMPOSE_ENV" >&2; exit 1; }

# The last assignment wins, and surrounding quotes are not part of the value
# (#1506).
env_value() {
  sed -n "s/^$1=//p" "$COMPOSE_ENV" | tail -1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

PROXY="$(env_value PROXY)"
case "${PROXY:-nginx}" in
  traefik)
    BACKEND=(-f docker-compose-prod.yml -f docker-compose-prod.traefik.yml -f docker-compose-prod.fork.yml)
    FRONTEND=(-p "$FRONTEND_PROJECT" -f docker-compose-prod.frontend.yml -f docker-compose-prod.frontend.traefik.yml -f docker-compose-prod.frontend.fork.yml)
    ;;
  nginx|"")
    BACKEND=(-f docker-compose-prod.yml -f docker-compose-prod.fork.yml)
    FRONTEND=(-p "$FRONTEND_PROJECT" -f docker-compose-prod.frontend.yml -f docker-compose-prod.frontend.fork.yml)
    ;;
  *)
    echo "PROXY=$PROXY in $COMPOSE_ENV is not one of: traefik, nginx" >&2
    exit 1
    ;;
esac

# A non-empty `SANDBOXD_TOKEN` is this host saying it runs a sandbox, and `up`
# without the profile would stop it (#1506).
PROFILES=()
if [ -n "$(env_value SANDBOXD_TOKEN)" ]; then
  PROFILES=(--profile sandbox)
  if [ -S /var/run/docker.sock ]; then
    DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
    export DOCKER_GID
  else
    echo "no /var/run/docker.sock, but SANDBOXD_TOKEN is set - the sandbox cannot start" >&2
    exit 1
  fi
fi

say "Fetching $REF"
git fetch --tags --quiet origin
git checkout --quiet --detach "$REF"
git --no-pager log --oneline -1

for overlay in docker-compose-prod.fork.yml docker-compose-prod.frontend.fork.yml; do
  if [ ! -f "$overlay" ]; then
    echo "$REF has no $overlay: it is older than the fork's server build, so deploy a later commit" >&2
    exit 1
  fi
done

SHA="$(git rev-parse HEAD)"
# Exported, not written to the env file, as `deploy.sh` does with its pin. After
# the deploy, the overlays' default - `latest` - is this build.
AGENTICOS_FORK_VERSION="sha-$(printf '%s' "$SHA" | cut -c1-7)"
export AGENTICOS_FORK_VERSION

# `< /dev/null` on every docker call that could read standard input (#1488).
compose() { docker compose --env-file "$COMPOSE_ENV" "$@" < /dev/null; }

# A tag already on the host - a redeploy, or a rollback to the previous build -
# is not built again. `--pull` takes the base images' current patch releases,
# as upstream's CI does by building on a fresh runner. BuildKit, because the
# backend's Dockerfile uses `RUN --mount`.
build_image() {
  local name="$1" dockerfile="$2" context="$3"
  local ref="$name:$AGENTICOS_FORK_VERSION"
  if docker image inspect "$ref" >/dev/null 2>&1; then
    echo "  $ref is already on this host"
    return 0
  fi
  DOCKER_BUILDKIT=1 docker build --pull \
    -f "$dockerfile" -t "$ref" \
    --label "org.opencontainers.image.revision=$SHA" \
    --label "org.opencontainers.image.source=$(git remote get-url origin)" \
    "$context" < /dev/null
}

# The same contexts and Dockerfiles as upstream's `images.yml`. The backend is
# built from the repository root, for `emails/compiled/`.
say "Building the images for $AGENTICOS_FORK_VERSION"
build_image agenticos-fork-backend backend/Dockerfile .
build_image agenticos-fork-frontend frontend/Dockerfile frontend

# One retry, and only when compose gave up on a dependency's health status from
# before this start (#1831).
compose_up() {
  local output
  if output=$(compose "$@" up -d 2>&1); then
    printf '%s\n' "$output"
    return 0
  fi
  printf '%s\n' "$output" >&2
  case "$output" in
    *"is unhealthy"*)
      say "A dependency answered with the health status it had before this start - waiting for its first probe"
      sleep 15
      compose "$@" up -d
      ;;
    *)
      return 1
      ;;
  esac
}

# The API and its migrations before the frontend. `pull` takes upstream's
# images - Postgres, Valkey, Prefect, the sandbox - and skips the fork's, which
# the overlays mark `pull_policy: never`.
say "Pulling upstream's images and starting the API"
compose "${BACKEND[@]}" "${PROFILES[@]+"${PROFILES[@]}"}" pull --quiet
compose_up "${BACKEND[@]}" "${PROFILES[@]+"${PROFILES[@]}"}"

say "Starting the frontend"
compose_up "${FRONTEND[@]}"

# `unhealthy` counts only once a probe has run since the container started.
wait_healthy() {
  local service="$1"; shift
  local cid attempt status started probed
  for attempt in $(seq 1 60); do
    # Re-read every round, and with `-a`: between restart attempts the container
    # is stopped and `ps -q` alone lists nothing, which would pin an empty id
    # for the whole wait.
    cid=$(compose "$@" ps -a -q "$service")
    status=$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo missing)
    case "$status" in
      healthy) echo "  $service: healthy"; return 0 ;;
      unhealthy)
        # `.End.Unix` is an integer out of the template; `StartedAt` is a string,
        # so it goes through `date`. A container with no probe yet has neither,
        # and waits.
        started=$(date -d "$(docker inspect -f '{{.State.StartedAt}}' "$cid")" +%s 2>/dev/null || echo 0)
        probed=$(docker inspect -f '{{range .State.Health.Log}}{{.End.Unix}} {{end}}' "$cid" 2>/dev/null | awk '{print $NF}')
        if [ -n "$probed" ] && [ "$probed" -gt "$started" ]; then
          echo "  $service: unhealthy" >&2
          compose "$@" logs --tail 50 "$service" >&2
          exit 1
        fi
        ;;
    esac
    if [ "$attempt" -eq 60 ]; then
      echo "  $service: still $status after 120s" >&2
      compose "$@" logs --tail 50 "$service" >&2
      exit 1
    fi
    sleep 2
  done
}

say "Waiting for health"
wait_healthy app "${BACKEND[@]}" "${PROFILES[@]+"${PROFILES[@]}"}"
wait_healthy frontend "${FRONTEND[@]}"

# Only now, so `latest` - what a compose command by hand runs - is always a
# build that came up healthy.
for image in agenticos-fork-backend agenticos-fork-frontend; do
  docker tag "$image:$AGENTICOS_FORK_VERSION" "$image:latest"
done

# The `sha-` tags past the newest $KEEP_BUILDS, never the one just deployed.
# Removing a tag removes the image's layers unless a kept build shares them.
prune_builds() {
  local image="$1" tag
  docker image ls "$image" --format '{{.CreatedAt}}|{{.Tag}}' \
    | sort -r | awk -F'|' '$2 ~ /^sha-/ { print $2 }' | tail -n +$((KEEP_BUILDS + 1)) \
    | while read -r tag; do
        if [ "$tag" != "$AGENTICOS_FORK_VERSION" ]; then
          docker image rm "$image:$tag" >/dev/null
          echo "  removed $image:$tag"
        fi
      done
}

say "Reclaiming space"
prune_builds agenticos-fork-backend
prune_builds agenticos-fork-frontend
docker image prune -f >/dev/null
# Build cache older than a week. Newer cache keeps the next build to the layers
# that changed.
docker builder prune -f --filter until=168h >/dev/null
df -h / | tail -1

say "Deployed $(git rev-parse --short HEAD) as $AGENTICOS_FORK_VERSION"
