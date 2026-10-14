# Zakupy i target wykonawcy — 0.40.30

Wcześniejszy target traktował możliwość przyszłego BUY jako uzupełnienie bilansu, a jednocześnie ograniczał import w tańszym oknie. Optymalizator nie mógł kupić wcześniej energii zastępującej droższy późniejszy zakup.

Wymagana trajektoria SOC i cele terminowe pozostają wynikiem bilansu zużycia, PV, sprawności, rezerw i dostępności BUY. Osobna lista opcjonalnych sufitów (`grid_charge_caps`) dopuszcza większy zakup w tańszym wcześniejszym slocie, jeżeli istnieje późniejszy droższy BUY. Podniesienie sufitu do limitu zapotrzebowania i fizycznej pojemności nie wymusza jego osiągnięcia. Optymalizator wybiera energię według kosztu całego horyzontu, z niezmienionymi ograniczeniami bilansu. Ta sama lista sufitów obowiązuje porównanie wariantu ze sprzedażą i bez sprzedaży.

Target wykonawczy jest wyprowadzany dopiero z wybranych przepływów. Dla nieprzerwanego ciągu rzeczywistych zakupów publikowany jest jeden najwyższy target od pierwszego slotu. Przerwa w zakupach rozpoczyna nowy ciąg. Cel końca okna jest sprawdzany osobno od opcjonalnego szczytu ładowania; zużycie po wcześniejszym osiągnięciu szczytu nie wymusza ponownego zakupu do tego szczytu.

## Walidacja

- Dziewięć nowych regresji: zastąpienie droższego późniejszego BUY; niewystarczająca moc tańszego okna; brak podniesienia dla droższego wcześniejszego BUY; wykluczenie SELL i brakującej ceny; opcjonalny sufit nie staje się wymogiem 100%; wspólny target kolejnych zakupów z rozdzieleniem po przerwie; PV może ładować ponad sufit importu; cena ujemna bez przyszłego zapotrzebowania nie tworzy dodatkowego zapasu; limit pojemności zachowuje konieczny późniejszy BUY.
- W syntetycznym teście 1,05/1,15 PLN/kWh: zakup dzienny rośnie z 1,4 do 3,8 kWh, nocny maleje z 2,4 do 0 kWh, koszt maleje z 4,28865 do 4,04865 PLN; końcowy SOC obu wariantów wynosi 40%. To wynik testu, nie odczyt instalacji.
- 380 testów usług: 375 przechodzi; cztery niepowodzenia i jeden błąd są identyczne na bazie 0.40.29 (371 testów). Dodatkowe 35 testów modułów i osiem funkcji testowych przechodzi. Łącznie 423 testów, 418 przechodzi. Testy wykonawcy: 22/22.
- Kompilacja Python i składnia obu skryptów JavaScript przechodzą. Test widoku sprawdza tabelę od najnowszego rekordu, chronologię osi wykresu oraz brak mutacji danych źródłowych.

Po ręcznej instalacji dodatku sprawdzić plan po replanie: ilość BUY i SOC w tańszym oknie, pozostałe droższe dokupienia, wspólny target ciągu, ciągłość SOC i cele zamknięcia doby. Niezastąpiony droższy zakup może wynikać z limitu pojemności, mocy, rezerw lub prognozowanego bilansu. Publikacja kodu nie jest potwierdzeniem jego działania w instalacji.
