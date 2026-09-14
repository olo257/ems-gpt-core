# EMS-GPT Core 0.31.3

## Krytyczna korekta semantyki SOC

- `soc_floor` chroni energię przed sprzedażą, ale nie blokuje autokonsumpcji.
- Zużycie domu może rozładowywać baterię do technicznego `battery_min_soc_pct`.
- `soc_target` wynika z bilansu energii do kolejnego zakupu lub pokrywającej
  deficyt nadwyżki PV, z uwzględnieniem zaplanowanego HP i sprawności baterii.
- Moc każdego przyszłego slotu zakupu jest ograniczona rzeczywistym limitem
  baterii; planer nie zakłada nieograniczonego uzupełnienia w jednym slocie.
- Usunięto stały cel 60% o 19:45 i historyczny target końca horyzontu.
- Zachowano techniczny limit SOC, ochronę TOU dla sprzedaży i brak zapisów do
  programów SOC Deye 1–6.
