# Executor TOU Grid conflict — release 0.40.29

Battery import ON and battery export OFF share the inverter TOU charging selector. The former dispatch order could enable Grid and subsequently overwrite it with Disabled while the global grid-charging switch remained enabled.

The fix dispatches OFF before ON, waits for named script services to finish, and preserves Grid on export OFF when grid charging is already active. A failed battery OFF prevents new battery ON commands in the same dispatch cycle. Planner behavior and configuration remain unchanged.

Validation: 22 executor tests pass, including four new conflict regressions. Across 414 tests, 409 pass; four failures and one error in analytics/planner contract tests also occur on baseline main. Module compilation passes.

Operator validation after installing 0.40.29: verify that an automatic BUY slot keeps the active TOU program on Grid and stops at its planned target.
