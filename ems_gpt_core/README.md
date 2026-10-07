# EMS-GPT Core

Wersja wydania: **0.39.25**.

Dokumenty obowiązujące:

- [`DOCS.md`](DOCS.md) — architektura, integracje, encje i procedura wydania;
- [`PLANNER_CONTRACT.md`](PLANNER_CONTRACT.md) — kanoniczny kontrakt planera;
- [`ENTITY_CATALOG.md`](ENTITY_CATALOG.md) — helpery i wszystkie encje HA używane przez kod;
- [`CHANGELOG.md`](CHANGELOG.md) — chronologiczna historia zmian;

Materiały dawnych wydań zostały przeniesione do [`docs/archive/`](docs/archive/)
i nie są bieżącą specyfikacją.

Najważniejsze reguły wykonawcze:

- energia PV jest przydzielana kolejno do: load domu wraz z HP, ładowania
  baterii, `PV_CWU`, `PV_EV`, a dopiero potem do eksportu;
- `PV_CWU` i `PV_EV` są poza load i poza targetem SOC; w trybie `AUTO`
  wymagają świeżej telemetrii, osiągniętego targetu i rzeczywistej nadwyżki PV;
- sprzedaż baterii wymaga dodatniej ceny sprzedaży oraz dodatniego wyniku
  ekonomicznego pełnego wariantu planu;
- PPD publikuje osobno `SELL_BAT` i `SELL_PV`; niedodatnia cena eksportu
  ustawia `NO_SELL_PV` bez blokowania CWU/EV;
- brak danych wykonawczych działa fail-safe i wyłącza odbiory elastyczne.
