# MUCA board v2 (MUCA2)

Readout board for printed stretchable mutual-capacitance skin sensors (reSkin / Skin-On), redesigned from the [MUCA](https://muca.cc/) board.

Status: schematic complete (ERC 0 errors), footprints placed, **not routed**. Rev 0.1.

## Architecture

| Block | Part | Notes |
|---|---|---|
| Touch AFE | FocalTech FT5316DME (QFN-48) | 21 TX x 12 RX = 252 nodes, I2C 0x38, <= 400 kHz. Same controller family as the original MUCA. |
| Host | ESP32-S3-MINI-1 | Native USB (CDC / JTAG), TWAI (CAN 2.0B). Radio unused while scanning. |
| USB | USB-C sink, USBLC6-2SC6 ESD | 5.1 kohm CC pull-downs. |
| CAN | SN65HVD230, NUP2105L TVS | 2x JST-GH 4-pin, DroneCAN pinout (1 +5 V, 2 CANH, 3 CANL, 4 GND). Solder-jumper 120 ohm termination. |
| Power | 2x LM66100 ideal-diode ORing (USB / CAN 5 V), AP2112K-3.3 LDO | Ferrite-filtered +3.3VA for the FT5316 analog supplies. Max input 5.5 V. |
| Sensor I/O | 2x Molex 200528 FPC, 1.0 mm pitch | J3: 23-pin TX, J4: 14-pin RX, GND guard on both edges. |
| Debug | 7 LEDs, RESET/BOOT, UART0 header, test points | LEDs: CAN pwr, USB pwr, 3V3, HB, SENSE, COMM, FAULT. |

## ESP32-S3 pin map

| GPIO | Function |
|---|---|
| IO0 | BOOT |
| IO4 / IO5 | CAN_TX / CAN_RX |
| IO7 | VBUS_SENSE (ADC1_CH6, VBUS x 15/25) |
| IO8 / IO9 | TP_SDA / TP_SCL |
| IO10 / IO11 | TP_INT / TP_RST (RST held low by 10 kohm until firmware releases it) |
| IO13-16 | LED HB / SENSE / COMM / FAULT |
| IO19 / IO20 | USB D- / D+ |

## Files

- `MUCA2.kicad_pro` - project (hierarchical: Power, MCU, Touch AFE, CAN)
- `MUCA2.kicad_sym` - project symbol library (FT5316DME, built from the FocalTech FT5x16 datasheet)
- Board: 70 x 50 mm outline, 2-layer assumed.

## Open points

- FT5316DME ships blank: flash `MuCaFirmware.bin` over I2C from the ESP32 ([muca-firmware](https://github.com/muca-board/muca-firmware)).
- FT5316DME sourcing not confirmed.
- Verify FPC and JST-GH opening direction in the 3D viewer before routing.
- No mounting holes yet.
- CAN bus power limited to 5 V nominal; a 12-24 V bus needs a buck converter.

Related: [Muca_lib](https://github.com/muca-board/Muca), [Muca_experiences](https://github.com/alex-crr/Muca_experiences).
