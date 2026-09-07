# ============================================================
# Makefile — Touchless PO-to-SO Pipeline
# ============================================================
# Usage:
#   make run          — Stage 1: full pipeline (unread emails)
#   make run-all      — Stage 1: all emails (not just unread)
#   make feedback     — Stage 2: Celonis SO results → notifications
#   make sync         — Sync Celonis master data to Azure Blob
#   make preflight    — Pre-push PO validation check
#   make exceptions   — Re-process failed/stuck POs (dry-run first)
#   make so-results   — Pull SO creation results from Celonis only
# ============================================================

PYTHON = python

# ── Stage 1 ────────────────────────────────────────────────────────────────────
run:
	$(PYTHON) src/pipeline/run_outlook_to_pipeline.py

run-all:
	$(PYTHON) src/pipeline/run_outlook_to_pipeline.py --all

skip-outlook:
	$(PYTHON) src/pipeline/run_outlook_to_pipeline.py --skip-outlook

# ── Stage 2 ────────────────────────────────────────────────────────────────────
feedback:
	$(PYTHON) src/pipeline/run_celonis_feedback.py --current-run

feedback-dry:
	$(PYTHON) src/pipeline/run_celonis_feedback.py --dry-run --current-run

# ── Celonis Sync ───────────────────────────────────────────────────────────────
sync:
	$(PYTHON) src/pipeline/celonis_to_azure.py

so-results:
	$(PYTHON) src/pipeline/celonis_to_azure.py --so-results-only

# ── Validation ─────────────────────────────────────────────────────────────────
preflight:
	$(PYTHON) src/pipeline/preflight_check.py

# ── Exception Handling ─────────────────────────────────────────────────────────
exceptions-dry:
	$(PYTHON) src/validation/exception_resolver.py --dry-run

exceptions:
	$(PYTHON) src/validation/exception_resolver.py

# ── Help ───────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  Touchless PO-to-SO Pipeline Commands"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make run           Stage 1: process unread PO emails"
	@echo "  make run-all       Stage 1: process ALL PO emails"
	@echo "  make feedback      Stage 2: Celonis SO results → notifications"
	@echo "  make sync          Sync Celonis master data to Azure Blob"
	@echo "  make so-results    Pull SO results from Celonis only"
	@echo "  make preflight     Validate POs before push"
	@echo "  make exceptions    Re-process stuck/failed POs"
	@echo "  ───────────────────────────────────────────────────────"
	@echo ""

.PHONY: run run-all skip-outlook feedback feedback-dry sync so-results preflight exceptions-dry exceptions help
