.PHONY: run down reset logs test test-unit postman demo evals mcp

run:            ## build and start everything (API :8080, mocks :8081, receiver :9000, Postgres :55432)
	docker compose up -d --build --wait
	@echo "API ready: http://localhost:8080/docs"

down:
	docker compose down

reset:          ## put mock upstream data back to its seed state
	curl -s -X POST http://localhost:8081/__reset

logs:
	docker compose logs -f api

test:           ## all tests (needs Postgres: started here if not running)
	docker compose up -d --wait postgres
	uv run pytest -q

test-unit:      ## no Docker needed
	uv run pytest -q tests/unit

postman:        ## run the Postman collection with newman
	npx newman run postman/alphacom.postman_collection.json -e postman/local.postman_environment.json

demo:           ## LLM answers the sample question through the MCP server (needs OPENAI_API_KEY)
	uv run --group demo python mcp/demo.py --follow-up

evals:          ## LLM conversation evals (needs OPENAI_API_KEY)
	uv run --group demo python mcp/evals/run_llm_evals.py --repeat 3

mcp:            ## run the MCP server on stdio (what Claude Desktop/Code launches)
	ALPHACOM_DEV_USER=alice uv run python mcp/server.py
