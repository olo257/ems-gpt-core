# Zbiorcza zmiana prognozowania zuzycia - plan integracji

Status: PR roboczy, bez podlaczenia do planera produkcyjnego.

## Potwierdzone mechanizmy
- `analytics_service.py`: profile po weekday/hour/minute, bez twardego limitu 7 dni.
- `materialization_service.py::learn_missing_load`: nadpisuje prognoze profilem z jedna probka, a fallback z 288 slotow splaszcza rozklad dobowy.
- `analytics_service.py::_native_load_kwh`: odejmuje cala pompe; `planner_service.py` zaklada, ze DHW jest w bazie, a HEAT dolicza osobno. To wymaga pogodzenia przed integracja.
- `planner_service.py`: dodatkowa korekta forecast_load_kwh i osobny HP_HEAT; nie wolno dokladac kolejnej korekty w nowym modelu.

## Docelowy kontrakt
1. Okno danych dla bieżącego profilu: maksymalnie 7 ostatnich pelnych dni; dane starsze tylko diagnostycznie.
2. Jeden profil 96 slotow: bazowy dom (z AGD), DHW tylko 06:00-08:00 i 18:00-20:00, HEAT osobno z przebiegiem malejacym od startu i warunkiem temperatury.
3. Bilans: E_total=E_native+E_DHW+E_HEAT; kazda skladowa rozlaczna. EV osobno zgodnie z istniejacym PPD.
4. Dla brakow telemetrii zachowaj ostatnia zweryfikowana prognoze, nie wpisuj zera i nie korzystaj ze starych profili.
5. Nowy model liczy prognoze tylko raz; bez nowego bufora AGD i bez mnozenia korekt.
6. Shadow: 7 kolejnych dni porownania prognoza/wynik (MAE, WAPE, bias) z dotychczasowym algorytmem, bez sterowania.
7. Widok WWW: wykres 24h prognoza/wynik, rozklad na komponenty, zrodlo prognozy, liczba probek, blad i pewnosc.
8. Widok ekonomiczny: bilans PV/zuzycie/bateria/BUY/SELL/SOC/ceny i wynik PLN, nie tylko porownanie.
9. Usunac widok odrzuconych planow dopiero po sprawdzeniu innych odwolujacych sie do niego komponentow; dane audytowe zachowac.
10. Testy: bez podwojnego DHW/HEAT, sloty 96, zmiana dnia, brak danych, skok AGD, HEAT rozruch, limit SOC i buy.

Nie scalać przed pelna integracja, testami i ocena regresji.
