# This fork's additions to upstream's `Makefile`. Fork-only.
#
# GNU make reads `GNUmakefile` before `Makefile`, so every `make` in this
# checkout runs this file, which includes upstream's `Makefile` unchanged.
# Upstream has no file by this name, so a sync never conflicts here.
#
# The prod targets run the fork's images: `make prod`, `make prod-frontend`,
# `make prod-down` and `make prod-logs` take the `*.fork.yml` overlays.
#
# They also take a server's own overlay when one exists beside them, last so it
# wins: `docker-compose-prod.local.yml` for the backend and
# `docker-compose-prod.frontend.local.yml` for the frontend. Never committed -
# they hold one host's settings, such as an external network an MCP server is
# on - and a missing one is skipped.

include Makefile

PROD_FILES += -f docker-compose-prod.fork.yml
PROD_FRONTEND_FILES += -f docker-compose-prod.frontend.fork.yml

PROD_FILES += $(addprefix -f ,$(wildcard docker-compose-prod.local.yml))
PROD_FRONTEND_FILES += $(addprefix -f ,$(wildcard docker-compose-prod.frontend.local.yml))
