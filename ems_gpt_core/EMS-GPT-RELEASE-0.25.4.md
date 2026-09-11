# EMS-GPT Core 0.25.4 — ręczna aktualizacja

## Zakres

- karta `HP_DHW` została usunięta z panelu Procesy;
- backend `HP_DHW` i skrypty „Wymuś ciepłą wodę” pozostają bez zmian;
- `HP_HEAT_DHW` pozostaje widoczny i działa według okien oraz nocnego progu temperatury.
- kliknięcie karty „Wykonawca” rozwija szybkie przyciski LIVE/OFF;
- tryb OFF jest fail-closed, a LIVE wymaga potwierdzenia i pełnej mapy bezpiecznych skryptów.

## Aktywacja wykonawcy

Sterowanie produkcyjne wymaga jednocześnie:

1. `executor_enabled=true`;
2. `executor_dry_run=false`;
3. `executor_activation_ack=EMS_CONNECTOR_ACCEPTED`.

Przed aktywacją należy sprawdzić mapowanie wszystkich skryptów oraz wyniki dry-run.
