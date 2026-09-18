# EMS-GPT Core 0.36.0

## Osobny moduł PPD odbiorników elastycznych

- `PV_CWU` i `PV_EV` są wyznaczane w `ppd_service.py` po zakończeniu obliczeń SOC.
- Oba PPD są tylko zgodą wykonawczą i nie uczestniczą w obliczaniu `soc_target`.
- Każde PPD tworzy jedno ciągłe okno dzienne aktualizowane przy każdym replanie.
- Automatyzacje Home Assistant nadal odpowiadają za rzeczywiste włączenie,
  wyłączenie, histerezę, temperaturę CWU, dostępność EV i kontrolę mocy.
- Chroniona wersja `production-0.35.8` pozostaje punktem powrotu.
