# Backend targets. `make help` lists them.
#
# Snyk runs at three checkpoints through the build (hours 1, 5 and 7), not once
# at the end: a high or critical finding discovered with an hour left is a score
# reduction we will not have time to fix.

PY := .venv/bin/python
PIP := .venv/bin/pip

.DEFAULT_GOAL := help
.PHONY: help setup test security verify status tables sync corpus serve tunnel loop chart graph-up graph-down clean

help:  ## List the targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t22

setup:  ## Install dependencies into .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

test:  ## Run the offline test suite (no network, no credentials)
	$(PY) -m unittest discover -s tests -v

security:  ## Snyk: dependencies and code. Run at hours 1, 5 and 7
	snyk test --file=requirements.txt --package-manager=pip
	snyk code test

verify:  ## Check all five services against the live APIs
	sh scripts/verify-setup.sh

status:  ## Backend health: graph size, corpus, runs
	$(PY) scripts/backend-setup.py --status

tables:  ## Create the applications and runs tables (idempotent)
	$(PY) scripts/backend-setup.py --tables

sync:  ## Merge the Cognee graph into the candidate graph
	$(PY) scripts/backend-setup.py --sync

corpus:  ## Rebuild the frozen corpus from data/raw, offline
	$(PY) scripts/backend-setup.py --corpus

serve:  ## Run the MCP server (streamable HTTP; set MCP_BEARER_TOKEN first)
	$(PY) -m mcp_server.server

tunnel:  ## Expose the MCP server for RocketRide staging
	cloudflared tunnel --url http://127.0.0.1:$${MCP_PORT:-8787}

loop:  ## Tick the day clock every 10 minutes through the RocketRide webhook
	$(PY) -m demo.loop --interval 600

loop-local:  ## Same loop in process. Debugging harness only, never the demo path
	$(PY) -m demo.loop --interval 600 --local --auto-feedback

chart:  ## Build data/chart.html from the runs table
	$(PY) -m demo.chart

graph-up:  ## Start the HydraDB OSS engine so the named queries run as real Cypher
	docker run -d --name hydradb-oss -p 7687:7687 -p 8443:8443 \
	  -e HYDRADB_AUTH=neo4j/$${GRAPH_BOLT_PASSWORD:-hackathon} hydradb/hydradb:latest
	@echo "now set GRAPH_BOLT_URL=bolt://localhost:7687 in .env"

graph-down:  ## Stop it
	docker rm -f hydradb-oss

clean:  ## Drop local state (graph cache, clock, pending questions)
	rm -rf data/state
