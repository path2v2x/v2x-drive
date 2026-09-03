.PHONY: help drive-web-dev drive-web-build drive-web-install drive-server-install drive-server-dry-run launch-drive

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Drive web (SvelteKit dashboard) ──

drive-web-install: ## Install dashboard dependencies
	cd apps/drive-web && npm ci

drive-web-dev: ## Start the dashboard dev server
	cd apps/drive-web && npm run dev

drive-web-build: ## Build the dashboard for production
	cd apps/drive-web && npm run build

# ── Drive server (Python/CARLA) ──

drive-server-install: ## Install drive server Python dependencies
	cd apps/drive-server && pip install -r requirements.txt

drive-server-dry-run: ## Run the drive server without CARLA
	cd apps/drive-server && python -m digital_twin_bridge.drive_main --dry-run

# ── Deployment ──

# ── Launch (GPU server) ──

launch-drive: ## Start drive server (requires CARLA running)
	./scripts/launch-drive.sh
