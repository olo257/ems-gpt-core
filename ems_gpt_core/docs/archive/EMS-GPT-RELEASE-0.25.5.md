# EMS-GPT Core 0.25.5 — ręczna aktualizacja

## Zakres

- rzeczywisty stan pompy cyrkulacyjnej na karcie `MANUAL_CIRCULATION`;
- bezpośredni odczyt `switch.sm_lite_1616r_2_pompa_cyrkulacyjna`;
- odświeżanie kontrolki co 5 sekund;
- poprawna etykieta `LIVE` aktywnego wykonawcy.
- naprawa błędu `HTTP 400` przy wywołaniu skryptów Home Assistant;
- jednoznaczny status `OFF` po wyłączeniu wykonawcy.

Zmiana nie uruchamia pompy i nie zmienia decyzji procesu.
