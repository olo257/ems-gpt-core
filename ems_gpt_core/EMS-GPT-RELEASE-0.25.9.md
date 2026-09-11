# EMS-GPT Core 0.25.9

- Usunięto procesy `MANUAL_CIRCULATION` i `HP_DHW` z planera, API override, wykonawcy i panelu Procesy.
- EMS-GPT nie odczytuje ani nie steruje przełącznikiem pompy cyrkulacyjnej.
- Usunięto sterowanie `Force DHW`; pozostaje wyłącznie proces `HP_HEAT_DHW`.
- `HP_HEAT_DHW` używa jednego ciągłego okna od pierwszego do ostatniego slotu źródłowego `HP = TAK`.
- Historyczne rekordy wycofanych procesów pozostają w MariaDB jako ślad audytowy.
