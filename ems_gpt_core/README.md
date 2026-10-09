# EMS-GPT Core

Wersja wydania: **0.39.32**.

Dokumenty obowiązujące:

- [`DOCS.md`](DOCS.md) — architektura, integracje, encje i procedura wydania;
- [`PLANNER_CONTRACT.md`](PLANNER_CONTRACT.md) — kanoniczny kontrakt planera;
- [`ENTITY_CATALOG.md`](ENTITY_CATALOG.md) — helpery i wszystkie encje HA używane przez kod;
- [`CHANGELOG.md`](CHANGELOG.md) — chronologiczna historia zmian;

Materiały dawnych wydań zostały przeniesione do [`docs/archive/`](docs/archive/)
i nie są bieżącą specyfikacją.

Najważniejsze reguły wykonawcze:

- energia PV pokrywa najpierw load domu z HP i ładuje baterię do 100%;
  `soc_target` ogranicza tylko import z sieci, nie ładowanie z PV; po target
  obowiązuje kolejność CWU → EV → sprzedaż PV przy cenie dodatniej → curtailment;
- `PV_CWU` i `PV_EV` są poza load i poza targetem SOC; w trybie `AUTO`
  wymagają świeżej telemetrii, osiągniętego targetu i rzeczywistej nadwyżki PV;
- sprzedaż baterii wymaga dodatniej ceny sprzedaży oraz dodatniego wyniku
  ekonomicznego pełnego wariantu planu;
- PPD publikuje osobno `SELL_BAT` i `SELL_PV`; niedodatnia cena eksportu
  ustawia `NO_SELL_PV` bez blokowania CWU/EV;
- brak danych wykonawczych działa fail-safe i wyłącza odbiory elastyczne.
