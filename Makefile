IMAGE ?= blast-radius-entry
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

CPU_LOG := $(CURDIR)/cpu.log

.PHONY: build sample check cpu

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Like sample, plus the CPU time the board adds: user and system of every
# process in the container, read from its cgroup's cpu.stat after the image's
# CMD exits. Rosetta on an Apple Silicon Mac makes it higher than the board's.
cpu: build
	cmd=$$(docker image inspect --format '{{join .Config.Cmd " "}}' $(IMAGE)) && \
	docker run $(RUN_FLAGS) --entrypoint /bin/sh $(IMAGE) -c \
		"$$cmd; status=\$$?; cat /sys/fs/cgroup/cpu.stat >&2; exit \$$status" \
		> $(OUTPUT) 2> $(CPU_LOG); \
	status=$$?; \
	grep -v -e '_usec ' -e '^nr_' $(CPU_LOG) >&2; \
	test $$status -eq 0 || exit $$status; \
	usec=$$(awk '$$1 == "usage_usec" { print $$2 }' $(CPU_LOG)); \
	echo "cpu.stat usage_usec $$usec" >&2; \
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) --cpu-seconds $$(awk "BEGIN { printf \"%.6f\", $$usec / 1000000 }")
