.PHONY: help sync fmt lint typecheck test test-all pipeline pipeline-consistency \
        openapi java verify verify-consistency report review clean \
        demo docker-up docker-down

UV_RUN := uv run

# Fixtures and the two systems the pipeline runs against. Each system gets its
# own output directory: mixing two catalogs in one file would make the rule ids
# and the Golden Master cases ambiguous.
VB6_SRC     := fixtures/vb6
CONS_SRC    := fixtures/consistency
VB6_OUT     := output/vb6
CONS_OUT    := output/consistency
VB6_GM      := catalog/golden-master/vb6.yaml
CONS_GM     := catalog/golden-master/consistency.yaml
ENTRY_VB6   := CustomerForm.ValidateCustomer
ENTRY_CONS  := ExecutarGrupo
# The reviewer recorded in the catalog when the demo opens the gate. A real
# project passes a person's name.
REVIEWER    ?= make-demo

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

sync: ## Install/sync the environment
	uv sync --all-extras

fmt: ## Format the code
	$(UV_RUN) ruff format src tests
	$(UV_RUN) ruff check --fix src tests

lint: ## Lint the code
	$(UV_RUN) ruff check src tests
	$(UV_RUN) ruff format --check src tests

typecheck: ## Static type check
	$(UV_RUN) mypy

test: ## Run the unit tests (no LLM, no Docker, no Java)
	$(UV_RUN) pytest tests/unit

test-all: ## Run every test, including the CLI integration tests
	$(UV_RUN) pytest

## ---------------------------------------------------------------- pipeline

# The human review gate is the point of this tool, so `pipeline` stops after
# listing what needs a decision. Run `make review` to see it.
pipeline: ## Analyze, slice and extract candidate rules (stops at the review gate)
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) analyze
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) clusters
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) slice \
		--entry $(ENTRY_VB6) --depth 5
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) extract-rules \
		--slice $(VB6_OUT)/slices/flow-customerform_validatecustomer.json
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) rules
	@echo
	@echo "Candidates are not in the contract until a human decides."
	@echo "Next: make review   (then: make contract make java make verify make report)"

review: ## List the candidates that still need a human decision
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) validate

## ---------------------------------------------------------------- artifacts

openapi: ## Generate the OpenAPI contract from the validated rules
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) contract

java: ## Contract -> Java 21 source, and build it to bytecode
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) codegen --compile

pipeline-consistency: ## The same stages for the consistency fixture
	$(UV_RUN) legacyctl --source $(CONS_SRC) --output $(CONS_OUT) analyze
	$(UV_RUN) legacyctl --source $(CONS_SRC) --output $(CONS_OUT) slice \
		--entry $(ENTRY_CONS) --depth 6
	$(UV_RUN) legacyctl --source $(CONS_SRC) --output $(CONS_OUT) extract-rules \
		--slice $(CONS_OUT)/slices/flow-executargrupo.json
	$(UV_RUN) legacyctl --source $(CONS_SRC) --output $(CONS_OUT) rules

verify: ## Golden Master vs the new system (VB6 fixture)
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) verify \
		--golden-master $(VB6_GM)

verify-consistency: ## Golden Master vs the new system (consistency fixture)
	$(UV_RUN) legacyctl --source $(CONS_SRC) --output $(CONS_OUT) verify \
		--golden-master $(CONS_GM)

report: ## Write legacy-report.md
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) report \
		--golden-master $(VB6_GM)

## ---------------------------------------------------------------- demo

# Opens the human review gate for demonstration only. Every rule records the
# reviewer and the note, so the catalog always shows the gate was opened on
# purpose. Do not use this on a real catalog.
demo: pipeline ## Run the whole chain, opening the review gate as $(REVIEWER)
	$(UV_RUN) legacyctl --source $(VB6_SRC) --output $(VB6_OUT) review-all \
		--decision approve --reviewer $(REVIEWER) \
		--note "demo: bulk approval, not a per-rule review"
	$(MAKE) openapi
	$(MAKE) java
	-$(MAKE) verify
	$(MAKE) report
	@echo
	@echo "Artifacts under $(VB6_OUT): graph/ slices/ catalog/ contracts/ java/ reports/"

## ---------------------------------------------------------------- chores

docker-up: ## Start the PostgreSQL used by the demo new system
	docker compose up -d

docker-down: ## Stop the demo stack
	docker compose down

clean: ## Remove generated artifacts
	rm -rf output .pytest_cache .mypy_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
