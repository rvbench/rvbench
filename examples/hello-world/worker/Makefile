PY ?= python3
BASE ?= http://127.0.0.1:18801

.PHONY: run probe native triton metrics clean

run:
	$(PY) worker.py --config config.json

probe:
	curl -s $(BASE)/capabilities | $(PY) -m json.tool

native:
	curl -s -X POST $(BASE)/jobs -H 'Content-Type: application/json' -d @job-native-c.json | $(PY) -m json.tool

triton:
	curl -s -X POST $(BASE)/jobs -H 'Content-Type: application/json' -d @job-triton.json | $(PY) -m json.tool

metrics:
	curl -s $(BASE)/metrics

clean:
	rm -rf data
