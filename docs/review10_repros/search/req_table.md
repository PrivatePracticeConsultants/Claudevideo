
### real store — seconds; 'first' = first time that URL is asked after a server start, 'again' = asked again at once

| request | base first (run 1 / run 2) | base again (run 1 / run 2) | prototype first / again | body identical to base | load (run 1) |
|---|---|---|---|---|---|
| rates — default | 0.11 / 0.12 | 0.07 / 0.07 | 0.11 / 0.08 | yes | 3.75 |
| summary — default | 0.31 / 0.29 | 0.00 / 0.00 | 0.30 / 0.00 | yes | 3.75 |
| rates — q full name | 0.11 / 0.12 | 0.07 / 0.07 | 0.11 / 0.05 | yes | 3.75 |
| summary — q full name | 0.11 / 0.10 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — q partial name | 0.11 / 0.14 | 0.07 / 0.08 | 0.09 / 0.06 | yes | 3.75 |
| summary — q partial name | 0.10 / 0.11 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — q one letter | 0.27 / 0.31 | 0.17 / 0.20 | 0.13 / 0.11 | yes | 3.75 |
| summary — q one letter | 0.30 / 0.31 | 0.00 / 0.00 | 0.22 / 0.00 | yes | 3.75 |
| rates — q two letters | 0.16 / 0.18 | 0.11 / 0.11 | 0.13 / 0.08 | yes | 3.75 |
| summary — q two letters | 0.16 / 0.16 | 0.00 / 0.00 | 0.11 / 0.00 | yes | 3.75 |
| rates — q 9-digit TIN | 0.04 / 0.04 | 0.02 / 0.03 | 0.05 / 0.03 | yes | 3.75 |
| summary — q 9-digit TIN | 0.03 / 0.03 | 0.00 / 0.00 | 0.03 / 0.00 | yes | 3.75 |
| rates — q 10-digit NPI | 0.03 / 0.02 | 0.01 / 0.01 | 0.03 / 0.02 | yes | 3.75 |
| summary — q 10-digit NPI | 0.02 / 0.02 | 0.00 / 0.00 | 0.02 / 0.00 | yes | 3.75 |
| rates — q partial digits | 0.12 / 0.11 | 0.06 / 0.07 | 0.07 / 0.05 | yes | 3.75 |
| summary — q partial digits | 0.10 / 0.10 | 0.00 / 0.00 | 0.05 / 0.00 | yes | 3.75 |
| rates — state | 0.15 / 0.16 | 0.09 / 0.10 | 0.11 / 0.08 | yes | 3.75 |
| summary — state | 0.14 / 0.13 | 0.00 / 0.00 | 0.09 / 0.00 | yes | 3.75 |
| rates — state + partial name | 0.12 / 0.12 | 0.09 / 0.07 | 0.11 / 0.07 | yes | 3.75 |
| summary — state + partial name | 0.11 / 0.10 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — cpt | 0.03 / 0.04 | 0.03 / 0.03 | 0.04 / 0.03 | yes | 3.75 |
| summary — cpt | 0.01 / 0.02 | 0.00 / 0.00 | 0.02 / 0.00 | yes | 3.75 |
| rates — cpt + partial name | 0.06 / 0.06 | 0.04 / 0.04 | 0.07 / 0.04 | yes | 3.75 |
| summary — cpt + partial name | 0.04 / 0.04 | 0.00 / 0.00 | 0.04 / 0.00 | yes | 3.75 |
| rates — payer + partial name | 0.09 / 0.07 | 0.04 / 0.04 | 0.09 / 0.05 | yes | 3.75 |
| summary — payer + partial name | 0.06 / 0.06 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — latest + full name | 0.11 / 0.12 | 0.06 / 0.07 | 0.08 / 0.05 | yes | 3.75 |
| summary — latest + full name | 0.09 / 0.09 | 0.00 / 0.00 | 0.06 / 0.00 | yes | 3.75 |
| rates — therapy only + partial name | 0.12 / 0.12 | 0.07 / 0.07 | 0.10 / 0.07 | yes | 3.75 |
| summary — therapy only + partial name | 0.10 / 0.12 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — hide outliers + full name | 0.11 / 0.12 | 0.06 / 0.06 | 0.10 / 0.06 | yes | 3.75 |
| summary — hide outliers + full name | 0.13 / 0.13 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — dollar off + full name | 0.12 / 0.09 | 0.06 / 0.05 | 0.08 / 0.06 | yes | 3.75 |
| summary — dollar off + full name | 0.09 / 0.09 | 0.00 / 0.00 | 0.06 / 0.00 | yes | 3.77 |
| rates — city + state | 0.15 / 0.16 | 0.10 / 0.09 | 0.11 / 0.07 | yes | 3.77 |
| summary — city + state | 0.12 / 0.13 | 0.00 / 0.00 | 0.10 / 0.00 | yes | 3.77 |
| rates — NPI grain, NPI | 1.14 / 0.51 | 0.26 / 0.25 | 0.52 / 0.26 | yes | 3.77 |
| summary — NPI grain, NPI | 0.61 / 0.63 | 0.00 / 0.00 | 0.61 / 0.00 | yes | 3.77 |
| rates — NPI grain, TIN | 0.20 / 0.21 | 0.11 / 0.12 | 0.20 / 0.11 | yes | 3.77 |
| summary — NPI grain, TIN | 0.17 / 0.15 | 0.00 / 0.00 | 0.16 / 0.00 | yes | 3.77 |
| rates — entity grain payer+cpt+name | 0.04 / 0.03 | 0.03 / 0.02 | 0.04 / 0.02 | yes | 3.77 |
| summary — entity grain payer+cpt+name | 0.03 / 0.04 | 0.00 / 0.00 | 0.03 / 0.00 | yes | 3.77 |
| rates — entity grain payer+cpt+state | 0.04 / 0.04 | 0.02 / 0.03 | 0.04 / 0.02 | yes | 3.77 |
| summary — entity grain payer+cpt+state | 0.04 / 0.03 | 0.00 / 0.00 | 0.03 / 0.00 | yes | 3.77 |
| rates — q full name, page 2 | 0.07 / 0.07 | 0.07 / 0.07 | 0.04 / 0.04 | yes | 3.77 |
| rates — q full name, sort name | 0.07 / 0.07 | 0.06 / 0.06 | 0.05 / 0.05 | yes | 3.77 |
| rates — q partial, sort payer asc | 0.07 / 0.07 | 0.07 / 0.06 | 0.06 / 0.06 | yes | 3.77 |
| rates — default sort name | 0.23 / 0.20 | 0.21 / 0.20 | 0.21 / 0.21 | yes | 3.77 |
| code page | 0.07 / 0.06 | 0.07 / 0.06 | 0.06 / 0.06 | yes | 3.77 |
| code page + state | 0.07 / 0.06 | 0.07 / 0.06 | 0.06 / 0.06 | yes | 3.77 |
| trend | 0.01 / 0.01 | 0.01 / 0.01 | 0.01 / 0.01 | yes | 3.77 |
| drawer tin | 0.11 / 0.11 | 0.11 / 0.10 | 0.11 / 0.11 | yes | 3.77 |
| drawer npi | 0.47 / 0.46 | 0.47 / 0.47 | 0.44 / 0.43 | yes | 3.79 |
| subjects '' | 0.01 / 0.02 | 0.01 / 0.01 | 0.01 / 0.01 | yes | 3.79 |
| subjects partial | 0.01 / 0.01 | 0.01 / 0.01 | 0.01 / 0.01 | yes | 3.79 |
| subjects TIN | 0.01 / 0.01 | 0.01 / 0.01 | 0.01 / 0.01 | yes | 3.79 |
| export.csv full name | 0.16 / 0.17 | 0.12 / 0.11 | 0.13 / 0.09 | yes | 3.79 |

57 of 57 response bodies byte-identical between base and prototype (real store).

### scale store — seconds; 'first' = first time that URL is asked after a server start, 'again' = asked again at once

| request | base first (run 1 / run 2) | base again (run 1 / run 2) | prototype first / again | body identical to base | load (run 1) |
|---|---|---|---|---|---|
| rates — default | 1.44 / 1.43 | 1.08 / 1.06 | 1.40 / 1.09 | yes | 5.23 |
| summary — default | 8.61 / 8.45 | 0.00 / 0.00 | 9.12 / 0.00 | yes | 4.74 |
| rates — q full name | 1.84 / 1.86 | 1.05 / 1.08 | 0.99 / 0.69 | yes | 4.76 |
| summary — q full name | 2.15 / 2.16 | 0.00 / 0.00 | 1.38 / 0.00 | yes | 4.76 |
| rates — q partial name | 2.24 / 2.30 | 1.48 / 1.48 | 1.11 / 0.77 | yes | 4.70 |
| summary — q partial name | 2.68 / 2.72 | 0.00 / 0.00 | 1.78 / 0.00 | yes | 4.70 |
| rates — q one letter | 6.92 / 7.15 | 5.46 / 5.71 | 2.40 / 1.78 | yes | 4.62 |
| summary — q one letter | 12.15 / 11.74 | 0.00 / 0.00 | 9.70 / 0.00 | yes | 4.30 |
| rates — q two letters | 7.71 / 7.18 | 6.09 / 5.56 | 2.43 / 1.90 | yes | 4.23 |
| summary — q two letters | 12.75 / 12.20 | 0.00 / 0.00 | 9.68 / 0.00 | yes | 4.18 |
| rates — q 9-digit TIN | 0.11 / 0.11 | 0.07 / 0.06 | 0.11 / 0.06 | yes | 4.18 |
| summary — q 9-digit TIN | 0.10 / 0.10 | 0.01 / 0.00 | 0.10 / 0.00 | yes | 4.18 |
| rates — q 10-digit NPI | 0.06 / 0.06 | 0.04 / 0.04 | 0.07 / 0.04 | yes | 4.18 |
| summary — q 10-digit NPI | 0.06 / 0.06 | 0.00 / 0.00 | 0.06 / 0.00 | yes | 4.18 |
| rates — q partial digits | 1.65 / 1.75 | 1.06 / 1.10 | 0.85 / 0.65 | yes | 4.18 |
| summary — q partial digits | 2.02 / 2.08 | 0.00 / 0.00 | 1.42 / 0.00 | yes | 4.17 |
| rates — state | 3.00 / 3.15 | 2.19 / 2.25 | 1.27 / 0.95 | yes | 4.15 |
| summary — state | 3.38 / 3.48 | 0.00 / 0.00 | 2.19 / 0.00 | yes | 4.15 |
| rates — state + partial name | 1.93 / 1.97 | 1.17 / 1.20 | 1.01 / 0.67 | yes | 4.14 |
| summary — state + partial name | 2.30 / 2.24 | 0.00 / 0.00 | 1.47 / 0.00 | yes | 4.14 |
| rates — cpt | 0.11 / 0.11 | 0.10 / 0.10 | 0.11 / 0.10 | yes | 4.14 |
| summary — cpt | 0.36 / 0.38 | 0.00 / 0.00 | 0.35 / 0.00 | yes | 4.14 |
| rates — cpt + partial name | 0.47 / 0.52 | 0.32 / 0.33 | 0.47 / 0.29 | yes | 4.13 |
| summary — cpt + partial name | 0.38 / 0.38 | 0.00 / 0.00 | 0.36 / 0.00 | yes | 4.13 |
| rates — payer + partial name | 1.04 / 1.04 | 0.62 / 0.61 | 0.63 / 0.37 | yes | 4.13 |
| summary — payer + partial name | 0.94 / 0.91 | 0.00 / 0.00 | 0.55 / 0.00 | yes | 4.13 |
| rates — latest + full name | 1.93 / 1.84 | 1.13 / 1.09 | 0.98 / 0.68 | yes | 4.12 |
| summary — latest + full name | 1.94 / 1.95 | 0.00 / 0.00 | 1.22 / 0.00 | yes | 4.12 |
| rates — therapy only + partial name | 2.37 / 2.26 | 1.51 / 1.48 | 1.10 / 0.81 | yes | 4.11 |
| summary — therapy only + partial name | 2.82 / 2.68 | 0.00 / 0.00 | 1.74 / 0.00 | yes | 4.10 |
| rates — hide outliers + full name | 2.16 / 2.22 | 1.08 / 1.12 | 1.50 / 0.71 | yes | 4.10 |
| summary — hide outliers + full name | 2.64 / 2.54 | 0.00 / 0.00 | 1.83 / 0.00 | yes | 4.09 |
| rates — dollar off + full name | 1.75 / 1.75 | 1.09 / 1.07 | 0.93 / 0.65 | yes | 4.09 |
| summary — dollar off + full name | 2.20 / 2.25 | 0.00 / 0.00 | 1.35 / 0.00 | yes | 4.08 |
| rates — city + state | 1.07 / 1.08 | 0.57 / 0.57 | 0.17 / 0.09 | yes | 4.08 |
| summary — city + state | 1.01 / 1.03 | 0.00 / 0.00 | 0.16 / 0.00 | yes | 4.08 |
| rates — NPI grain, NPI | 0.10 / 0.11 | 0.05 / 0.06 | 0.11 / 0.06 | yes | 4.08 |
| summary — NPI grain, NPI | 0.13 / 0.15 | 0.00 / 0.00 | 0.16 / 0.00 | yes | 4.08 |
| rates — NPI grain, TIN | 0.39 / 0.42 | 0.22 / 0.24 | 0.44 / 0.24 | yes | 4.08 |
| summary — NPI grain, TIN | 0.34 / 0.36 | 0.00 / 0.00 | 0.38 / 0.00 | yes | 4.08 |
| rates — entity grain payer+cpt+name | 0.85 / 0.65 | 0.48 / 0.41 | 0.86 / 0.50 | yes | 4.08 |
| summary — entity grain payer+cpt+name | 0.61 / 0.50 | 0.00 / 0.00 | 0.63 / 0.00 | yes | 4.08 |
| rates — entity grain payer+cpt+state | 0.30 / 0.28 | 0.18 / 0.18 | 0.30 / 0.18 | yes | 4.08 |
| summary — entity grain payer+cpt+state | 0.25 / 0.25 | 0.00 / 0.00 | 0.26 / 0.00 | yes | 4.08 |
| rates — q full name, page 2 | 1.83 / 1.80 | 1.12 / 1.15 | 0.96 / 0.65 | yes | 4.07 |
| rates — q full name, sort name | 1.13 / 1.14 | 1.16 / 1.11 | 0.74 / 0.72 | yes | 4.07 |
| rates — q partial, sort payer asc | 2.37 / 2.31 | 1.46 / 1.50 | 1.20 / 0.93 | yes | 4.06 |
| rates — default sort name | 4.60 / 4.65 | 4.34 / 4.66 | 4.55 / 4.33 | yes | 4.05 |
| code page | 0.84 / 0.96 | 0.91 / 0.90 | 0.78 / 0.84 | yes | 4.05 |
| code page + state | 0.48 / 0.48 | 0.45 / 0.49 | 0.43 / 0.43 | yes | 4.05 |
| trend | 0.15 / 0.17 | 0.15 / 0.17 | 0.14 / 0.14 | yes | 4.05 |
| drawer tin | 0.32 / 0.34 | 0.29 / 0.30 | 0.29 / 0.32 | yes | 4.05 |
| drawer npi | 0.09 / 0.12 | 0.09 / 0.12 | 0.11 / 0.11 | yes | 4.05 |
| subjects '' | 0.02 / 0.03 | 0.02 / 0.02 | 0.03 / 0.02 | yes | 4.05 |
| subjects partial | 0.02 / 0.03 | 0.02 / 0.03 | 0.02 / 0.02 | yes | 4.05 |
| subjects TIN | 0.02 / 0.02 | 0.02 / 0.02 | 0.02 / 0.02 | yes | 4.05 |
| export.csv full name | 2.21 / 2.34 | 1.94 / 1.97 | 1.48 / 1.18 | yes | 4.05 |

57 of 57 response bodies byte-identical between base and prototype (scale store).
