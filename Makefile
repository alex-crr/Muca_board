PYTHON ?= python3
VERSION ?= local
OUT_DIR ?= build
OFFLINE ?=

.PHONY: help check check-nofail sanity jlcpcb bom-price docs docs-fast ibom all clean

help:
	@echo "KiCad 10 Local CI Targets:"
	@echo "  make check          - Run ERC & DRC (non-blocking by default; see ci/config.yml)"
	@echo "  make sanity         - Run BOM LCSC/Value conflict & library table sanity checks"
	@echo "  make jlcpcb         - Generate JLCPCB Gerbers, merged BOM & CPL"
	@echo "  make bom-price      - Fetch live JLC/LCSC pricing, thumbnails & interactive bom.html"
	@echo "  make ibom           - Generate InteractiveHtmlBom (requires InteractiveHtmlBom installed)"
	@echo "  make docs           - Export Schematic PDF, 2D/3D renders, STEP & build Pages site"
	@echo "  make docs-fast      - Recompile docs/*.md reusing existing build/docs/site (no KiCad)"
	@echo "  make all            - Run full local release pipeline into $(OUT_DIR)/"
	@echo "  make clean          - Remove $(OUT_DIR)/"

check:
	$(PYTHON) ci/scripts/parse_checks.py --out-dir $(OUT_DIR)/reports

check-nofail:
	$(PYTHON) ci/scripts/parse_checks.py --out-dir $(OUT_DIR)/reports --no-fail

jlcpcb:
	$(PYTHON) ci/scripts/build_jlcpcb.py --out-dir $(OUT_DIR)/jlcpcb --version $(VERSION)

sanity: jlcpcb
	$(PYTHON) ci/scripts/sanity_checks.py \
		--bom-json $(OUT_DIR)/jlcpcb/bom-merged.json \
		--cpl-csv $(OUT_DIR)/jlcpcb/muca2-$(VERSION)-jlcpcb-cpl.csv \
		--out-dir $(OUT_DIR)/reports

bom-price: jlcpcb
	$(PYTHON) ci/scripts/jlc_bom_pricing.py \
		--bom-json $(OUT_DIR)/jlcpcb/bom-merged.json \
		--out-dir $(OUT_DIR)/bom \
		--version $(VERSION) \
		$(if $(OFFLINE),--offline,)

ibom:
	mkdir -p $(OUT_DIR)/ibom
	INTERACTIVE_HTML_BOM_NO_DISPLAY=1 generate_interactive_bom \
		--no-browser --dark-mode \
		--extra-fields "Mouser Part Number,Manufacturer_Name,Manufacturer_Part_Number" \
		--dest-dir $(OUT_DIR)/ibom \
		--name-format "ibom" \
		MUCA2.kicad_pcb

docs: jlcpcb sanity bom-price
	$(PYTHON) ci/scripts/build_docs.py \
		--out-dir $(OUT_DIR)/docs \
		--version $(VERSION) \
		--version-id . \
		--jlcpcb-dir $(OUT_DIR)/jlcpcb \
		--bom-json $(OUT_DIR)/bom/bom-priced.json \
		--part-images-dir $(OUT_DIR)/bom/part-images \
		--bom-csv $(OUT_DIR)/bom/muca2-$(VERSION)-bom-priced.csv \
		$(if $(wildcard $(OUT_DIR)/ibom/ibom.html),--ibom $(OUT_DIR)/ibom/ibom.html,) \
		$(if $(wildcard $(OUT_DIR)/reports/checks-summary.md),--checks-md $(OUT_DIR)/reports/checks-summary.md,) \
		$(if $(wildcard $(OUT_DIR)/reports/checks-summary.json),--checks-json $(OUT_DIR)/reports/checks-summary.json,) \
		$(if $(wildcard $(OUT_DIR)/reports/sanity-summary.md),--sanity-md $(OUT_DIR)/reports/sanity-summary.md,)

docs-fast:
	$(PYTHON) ci/scripts/build_docs.py \
		--out-dir $(OUT_DIR)/docs-rebuilt \
		--version $(VERSION) \
		--version-id . \
		--reuse-site $(OUT_DIR)/docs/site

all: check jlcpcb sanity bom-price docs

clean:
	rm -rf $(OUT_DIR)
