# This fork's additions to upstream's `Makefile`. Fork-only.
#
# GNU make reads `GNUmakefile` before `Makefile`, so every `make` in this
# checkout runs this file, which includes upstream's `Makefile` unchanged.
# Upstream has no file by this name, so a sync never conflicts here.
#
# The prod targets run the fork's images: `make prod`, `make prod-frontend`,
# `make prod-down` and `make prod-logs` take the `*.fork.yml` overlays.

include Makefile

PROD_FILES += -f docker-compose-prod.fork.yml
PROD_FRONTEND_FILES += -f docker-compose-prod.frontend.fork.yml
