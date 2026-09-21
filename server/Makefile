APP     := marquee
IMAGES  := marquee-api marquee-player marquee-web
VERSION := $(shell cat VERSION)
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo nogit)
PORT    := 8410

.DEFAULT_GOAL := help

help:  ## show this menu
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

up:  ## start
	docker compose up -d
up-build:  ## rebuild + start (the code-changed path)
	docker compose up -d --build
	$(MAKE) tag-images
build-only:  ## build without tagging (used by release)
	docker compose build
down:  ## stop
	docker compose down
clean:  ## stop + remove orphans
	docker compose down --remove-orphans
pull:  ## refresh upstream images
	docker compose pull && docker compose up -d
restart:  ## restart all
	docker compose restart
logs:  ## follow logs
	docker compose logs -f
logs-be:  ## follow api logs
	docker compose logs -f api
ps: status
status:  ## container status
	docker compose ps
health:  ## hit the health endpoint
	@curl -sf http://localhost:$(PORT)/healthz && echo || echo "unhealthy"
migrate:  ## apply database migrations (alembic upgrade head)
	docker compose run --rm api alembic upgrade head
db-revision:  ## autogenerate a migration: make db-revision M="add panels.foo"
	docker compose run --rm api alembic revision --autogenerate -m "$(M)"
db-current:  ## show the revision the database is at
	docker compose run --rm api alembic current
be-test:  ## backend tests
# Runs in the PLAYER container, not api. tests/test_multicard_stream.py imports
# player.player, and the api image ships app/ alembic/ scripts/ tests/ only --
# so those two tests could never pass there and be-test was permanently red.
# The player image carries both player and marquee_core. PYTHONPATH=/app is
# needed because pytest's rootdir is the mounted tests dir, which leaves /app
# off sys.path even though an interactive `python -c "import player"` works.
# tests/ is mounted; everything under test stays the baked image.
#
# FIRMWARE is mounted too when it is there, because tests/test_wire.py asserts
# that the panel firmware's wire constants match this side's. Without it those
# tests SKIP, and a skipped test is not a test -- the drift it exists to catch
# shows up as a sheared image rather than an error, so it has to actually run.
# The default is the combined repo's layout (server/ and firmware/ side by
# side); point FIRMWARE elsewhere for a separate checkout.
#
# doc/ is mounted for the same reason: one test imports every ```python block
# in doc/providers.md, because a reference whose examples do not run teaches
# the wrong thing twice. The player image has no reason to carry docs, so the
# test would skip -- and a skipped test is not a test.
	docker compose run --rm -e PYTHONPATH=/app -e LEDWALL_FIRMWARE=/firmware \
	  -v "$(CURDIR)/tests:/app/tests:ro" \
	  -v "$(CURDIR)/doc:/app/doc:ro" \
	  $(if $(wildcard $(FIRMWARE)/sw_rust),-v "$(abspath $(FIRMWARE)):/firmware:ro",) \
	  player pytest -q /app/tests
tag-images:  ## stamp :VERSION and :sha onto built images
	@for i in $(IMAGES); do \
	  docker tag $$i:latest $$i:$(VERSION) 2>/dev/null || true; \
	  docker tag $$i:latest $$i:$(GIT_SHA) 2>/dev/null || true; \
	done; echo "tagged $(VERSION) / $(GIT_SHA)"
SITE ?= dashboard

# Where the panel firmware checkout lives, for the cross-half wire test.
# Defaults to the combined repo's layout: server/ and firmware/ as siblings.
FIRMWARE ?= $(CURDIR)/../firmware

shows-export:  ## export shows from the DB to custom/$(SITE)/shows/
	@docker compose exec -T api python /app/scripts/shows.py export /custom/$(SITE)/shows

shows-check:  ## render every show and report what is broken
# Runs in the player container: it has ffmpeg, the media shares and the bus.
# Exits non-zero if any show fails to parse, raises, or reports a layer error.
	docker compose run --rm -e PYTHONPATH=/app \
	  -v "$(CURDIR)/scripts:/app/scripts:ro" player python /app/scripts/check_shows.py

shows-import:  ## import custom/$(SITE)/shows/ into the DB (idempotent)
	@docker compose exec -T api python /app/scripts/shows.py import /custom/$(SITE)/shows

push:  ## push HEAD + tags to ALL remotes
	@git remote | grep -q '^origin$$' || { echo "no canonical remote"; exit 1; }
	@for r in $$(git remote); do echo "-> $$r"; git push $$r HEAD --follow-tags; done
release:  ## make release V=X.Y.Z
	@test -n "$(V)" || { echo "usage: make release V=X.Y.Z"; exit 1; }
	@echo "$(V)" > VERSION
	@$(MAKE) build-only || { git checkout VERSION; exit 1; }
	@git add -A && git commit -m "v$(V)" && git tag -a v$(V) -m "v$(V)"
	@$(MAKE) tag-images && $(MAKE) push

.PHONY: shows-check help up up-build build-only down clean pull restart logs logs-be ps status health be-test migrate db-revision db-current tag-images push release
