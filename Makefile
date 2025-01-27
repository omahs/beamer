.PHONY: dist-exe container-image relayers all lint black format docs clean

CODE_DIRS = beamer/ scripts/
CONTRACTS = "contracts/**/*.sol"

all: lint

lint:
	mypy $(CODE_DIRS)
	black --check --diff $(CODE_DIRS)
	flake8 $(CODE_DIRS)
	isort $(CODE_DIRS) --diff --check-only
	pylint $(CODE_DIRS)
	npx solhint $(CONTRACTS)
	npx prettier --list-different $(CONTRACTS)

black:
	black $(CODE_DIRS)

format: black
	isort $(CODE_DIRS)
	npx prettier --write $(CONTRACTS)

dist-exe:
	shiv -c beamer-agent -o dist/beamer-agent .

container-image: relayers
	DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile.agent -t beamer-agent .

relayers:
	yarn --cwd relayer install
	yarn --cwd relayer build
	yarn --cwd relayer add pkg
	yarn --cwd relayer run pkg -t node16-linux-x64 -o relayer-node16-linux-x64 build/src/service.js
	yarn --cwd relayer run pkg -t node16-macos-x64 -o relayer-node16-macos-x64 build/src/service.js

docs:
	make -C docs html

clean:
	make -C docs clean
