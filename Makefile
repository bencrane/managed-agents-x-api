.PHONY: dev

dev:
	doppler run -- uvicorn app.main:app --reload
