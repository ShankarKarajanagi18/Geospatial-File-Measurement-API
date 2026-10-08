.PHONY: install run test lint samples docker

install:
	pip install -r requirements-dev.txt

run:
	uvicorn app.main:app --reload

test:
	pytest

lint:
	ruff check .

samples:
	python scripts/make_sample_data.py

docker:
	docker compose up --build
