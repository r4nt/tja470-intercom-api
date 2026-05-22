.PHONY: pypi build upload clean test

clean:
	rm -rf dist/ build/ *.egg-info

test:
	.venv/bin/pytest -v

build: clean
	.venv/bin/python -m build

upload:
	.venv/bin/python -m twine upload dist/*

pypi: build upload
