## KiCad 10 ERC & DRC Report — `MUCA2`

> [!WARNING]
> **0 errors**, but **427 warning(s)** detected (129 ERC, 298 DRC).

- **KiCad Version:** `10.0.8`
- **Gate Policy:** Non-blocking — errors are highlighted via alerts & GitHub annotations without stopping the build.

| Check | Status | Errors | Warnings | Exclusions |
| :--- | :---: | :---: | :---: | :---: |
| **ERC** (`MUCA2.kicad_sch`) | ✅ PASS | **0** | 129 | 0 |
| **DRC** (`MUCA2.kicad_pcb`) | ✅ PASS | **0** | 298 | 0 |
| ↳ PCB Rules | — | 0 | 143 | — |
| ↳ Unconnected Items | — | 0 | 0 | — |
| ↳ Schematic Parity | — | 0 | 155 | — |

<details><summary>⚠️ ERC Warnings</summary>

- `lib_symbol_issues`: **86**
- `footprint_link_issues`: **43**

1. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'power' (Symbol #PWR0105 [+3V3] @ (66.04, 86.36 mm))
2. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'power' (Symbol #FLG0102 [PWR_FLAG] @ (76.20, 86.36 mm))
3. **`footprint_link_issues`** [/]: The current configuration does not include the footprint library 'Capacitor_SMD' (Symbol C2 [C] @ (78.74, 64.77 mm))
4. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'Device' (Symbol C2 [C] @ (78.74, 64.77 mm))
5. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'power' (Symbol #PWR0101 [GND] @ (78.74, 73.66 mm))
6. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'power' (Symbol #PWR0102 [GND] @ (88.90, 73.66 mm))
7. **`footprint_link_issues`** [/]: The current configuration does not include the footprint library 'Inductor_SMD' (Symbol FB1 [FerriteBead_Small] @ (88.90, 86.36 mm))
8. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'Device' (Symbol FB1 [FerriteBead_Small] @ (88.90, 86.36 mm))
9. **`footprint_link_issues`** [/]: The current configuration does not include the footprint library 'Package_TO_SOT_SMD' (Symbol U3 [LM66100DCK] @ (101.60, 63.50 mm))
10. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'Power_Management' (Symbol U3 [LM66100DCK] @ (101.60, 63.50 mm))
11. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'power' (Symbol #PWR0103 [GND] @ (101.60, 73.66 mm))
12. **`footprint_link_issues`** [/]: The current configuration does not include the footprint library 'Capacitor_SMD' (Symbol C5 [C] @ (101.60, 90.17 mm))
13. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'Device' (Symbol C5 [C] @ (101.60, 90.17 mm))
14. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'power' (Symbol #PWR0107 [GND] @ (101.60, 99.06 mm))
15. **`lib_symbol_issues`** [/]: The current configuration does not include the symbol library 'power' (Symbol #PWR0106 [+3.3VA] @ (109.22, 86.36 mm))

_...and 114 more (see full JSON report artifact)._

</details>

<details><summary>⚠️ DRC Warnings</summary>

- `net_conflict`: **107**
- `silk_edge_clearance`: **70**
- `lib_footprint_issues`: **43**
- `footprint_symbol_field_mismatch`: **43**
- `silk_over_copper`: **20**
- `silk_overlap`: **9**
- `footprint_symbol_mismatch`: **5**
- `text_height`: **1**

1. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (141.61, 96.91 mm))
2. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (144.21, 96.91 mm))
3. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (146.75, 96.91 mm))
4. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (149.29, 96.91 mm))
5. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (151.83, 96.91 mm))
6. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (154.37, 96.91 mm))
7. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (156.91, 96.91 mm))
8. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (159.45, 96.91 mm))
9. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (161.99, 96.91 mm))
10. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (164.53, 96.91 mm))
11. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (164.65, 96.91 mm))
12. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (164.77, 96.91 mm))
13. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (164.88, 96.91 mm))
14. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (165.00, 96.91 mm))
15. **`silk_edge_clearance`**: Silkscreen clipped by board edge (Segment on Edge.Cuts @ (102.00, 100.00 mm) — Segment of J4 on F.Silkscreen @ (165.12, 96.91 mm))

_...and 283 more (see full JSON report artifact)._

</details>
