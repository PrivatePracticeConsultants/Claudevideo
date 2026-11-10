| request | scale_base first / second (s) | scale_proto first / second (s) | bytes | byte-identical |
|---|---|---|---|---|
| rates | default | 1.15 / 1.10 | 1.47 / 1.10 | 71320 | yes |
| summary | default | 0.00 / 0.00 | 9.61 / 0.00 | 3558 | yes |
| rates | q full name | 1.79 / 1.06 | 1.39 / 0.74 | 71374 | yes |
| summary | q full name | 2.28 / 0.00 | 1.50 / 0.00 | 3757 | yes |
| rates | q partial name | 2.33 / 1.49 | 1.53 / 1.04 | 71327 | yes |
| summary | q partial name | 2.85 / 0.00 | 1.87 / 0.00 | 3693 | yes |
| rates | q one letter | 7.79 / 5.50 | 6.86 / 5.25 | 71320 | yes |
| summary | q one letter | 12.24 / 0.00 | 11.61 / 0.00 | 3558 | yes |
| rates | q two letters | 7.57 / 6.08 | 6.87 / 5.80 | 71320 | yes |
| summary | q two letters | 12.72 / 0.00 | 11.48 / 0.00 | 3558 | yes |
| rates | q 9-digit TIN | 0.11 / 0.07 | 0.14 / 0.08 | 70597 | yes |
| summary | q 9-digit TIN | 0.10 / 0.00 | 0.10 / 0.00 | 3380 | yes |
| rates | q 10-digit NPI | 0.06 / 0.04 | 0.06 / 0.04 | 60 | yes |
| summary | q 10-digit NPI | 0.06 / 0.00 | 0.06 / 0.00 | 375 | yes |
| rates | q partial digits | 1.65 / 1.08 | 1.19 / 0.81 | 71222 | yes |
| summary | q partial digits | 2.14 / 0.00 | 1.58 / 0.00 | 3588 | yes |
| rates | state | 3.15 / 2.31 | 2.37 / 1.76 | 71281 | yes |
| summary | state | 3.55 / 0.00 | 2.62 / 0.00 | 3609 | yes |
| rates | state + partial name | 2.02 / 1.21 | 1.12 / 0.81 | 71344 | yes |
| summary | state + partial name | 2.43 / 0.00 | 1.63 / 0.00 | 3658 | yes |
| rates | cpt | 0.15 / 0.12 | 0.11 / 0.10 | 71267 | yes |
| summary | cpt | 0.38 / 0.00 | 0.38 / 0.00 | 514 | yes |
| rates | cpt + partial name | 0.50 / 0.32 | 0.52 / 0.32 | 71328 | yes |
| summary | cpt + partial name | 0.39 / 0.00 | 0.36 / 0.00 | 524 | yes |
| rates | payer + partial name | 1.00 / 0.60 | 0.73 / 0.48 | 71306 | yes |
| summary | payer + partial name | 0.91 / 0.00 | 0.65 / 0.00 | 3615 | yes |
| rates | latest + full name | 1.94 / 1.13 | 1.16 / 0.72 | 71406 | yes |
| summary | latest + full name | 1.93 / 0.00 | 1.15 / 0.00 | 3694 | yes |
| rates | therapy only + partial name | 2.31 / 1.54 | 1.59 / 1.19 | 71327 | yes |
| summary | therapy only + partial name | 2.67 / 0.00 | 1.97 / 0.00 | 3693 | yes |
| rates | hide outliers + full name | 2.23 / 1.10 | 1.50 / 0.77 | 71374 | yes |
| summary | hide outliers + full name | 2.61 / 0.00 | 1.88 / 0.00 | 3757 | yes |
| rates | dollar off + full name | 1.81 / 1.07 | 1.05 / 0.74 | 71374 | yes |
| summary | dollar off + full name | 2.29 / 0.00 | 1.46 / 0.00 | 3757 | yes |
| rates | city + state | 1.04 / 0.54 | 0.33 / 0.17 | 60 | yes |
| summary | city + state | 1.01 / 0.00 | 0.25 / 0.00 | 375 | yes |
| rates | NPI grain, NPI | 0.14 / 0.06 | 0.11 / 0.07 | 8057 | yes |
| summary | NPI grain, NPI | 0.15 / 0.00 | 0.13 / 0.00 | 987 | yes |
| rates | NPI grain, TIN | 0.45 / 0.23 | 0.40 / 0.25 | 66946 | yes |
| summary | NPI grain, TIN | 0.37 / 0.00 | 0.37 / 0.00 | 3404 | yes |
| rates | entity grain payer+cpt+name | 0.79 / 0.43 | 0.81 / 0.41 | 68493 | yes |
| summary | entity grain payer+cpt+name | 0.51 / 0.00 | 0.52 / 0.00 | 519 | yes |
| rates | entity grain payer+cpt+state | 0.28 / 0.18 | 0.27 / 0.17 | 68391 | yes |
| summary | entity grain payer+cpt+state | 0.24 / 0.00 | 0.24 / 0.00 | 535 | yes |
| rates | q full name, page 2 | 1.79 / 1.15 | 1.05 / 0.70 | 71445 | yes |
| rates | q full name, sort name | 1.13 / 1.10 | 0.73 / 0.74 | 71021 | yes |
| rates | q partial, sort payer asc | 2.30 / 1.49 | 1.52 / 1.10 | 177088 | yes |
| rates | default sort name | 4.73 / 4.40 | 4.52 / 4.66 | 70572 | yes |
| code page | 0.90 / 0.84 | 0.86 / 0.98 | 123484 | yes |
| code page + state | 0.45 / 0.49 | 0.46 / 0.48 | 124257 | yes |
| trend | 0.18 / 0.17 | 0.15 / 0.18 | 4692 | yes |
| drawer tin | 0.33 / 0.31 | 0.33 / 0.30 | 93809 | yes |
| drawer npi | 0.12 / 0.10 | 0.11 / 0.11 | 9110 | yes |
| subjects '' | 0.02 / 0.02 | 0.02 / 0.03 | 7426 | yes |
| subjects partial | 0.02 / 0.02 | 0.02 / 0.02 | 7480 | yes |
| subjects TIN | 0.02 / 0.02 | 0.02 / 0.02 | 154 | yes |
| export.csv full name | 2.11 / 1.98 | 1.50 / 1.21 | 4567762 | yes |

57 of 57 bodies byte-identical
