# MUCA board v2 (MUCA2)

Readout board for printed stretchable mutual-capacitance skin sensors (reSkin / Skin-On), redesigned from the [MUCA](https://muca.cc/) board.

Status: rev 0.1, schematic complete (ERC clean), board routed (DRC 0 errors, silkscreen warnings only). Not fabricated.

## Architecture

| Block | Part | Notes |
|---|---|---|
| Touch AFE | FocalTech FT5316DME (QFN-48) | 21 TX x 12 RX = 252 nodes, I2C 0x38, <= 400 kHz. Same controller family as the original MUCA. |
| Host | Seeed Studio XIAO ESP32S3 (SMD castellated) | USB-C, 3.3 V regulator, RESET/BOOT buttons on the module. TWAI (CAN 2.0B). |
| CAN | SN65HVD230, NUP2105L TVS | 2x JST-GH 4-pin, DroneCAN pinout (1 +5 V, 2 CANH, 3 CANL, 4 GND). Solder-jumper 120 ohm termination. |
| Power | LM66100 ideal diode CAN 5 V -> XIAO 5V pin | USB powers the board through the XIAO; CAN 5 V back-feeds through the diode. XIAO 3V3 powers the whole board; ferrite-filtered +3.3VA for the FT5316 analog supplies. Max input 5.5 V. |
| Sensor I/O | 2x 2.54 mm right-angle female socket, 1x10 | J3: TX1-TX10, J4: RX1-RX10 (10 x 10), pin 1 at the right end. Sensor tails carry Amphenol Clincher 66226 pin connectors (e.g. 66226-037LF, 5 pos) on pins 1-5. |
| Debug | 7 LEDs, 7 test points | LEDs, labelled on silkscreen: CAN PWR, 3V3, TOUCH PWR (FT5316 analog rail), and the status row FAULT / COMM / SENSE / HEARTBEAT (left to right). Test points on +5V, +3V3, GND and the I2C/INT/RST bus. |

## Layout

70 x 50 mm, 2 layers. Sensing side (sensor sockets + FT5316) on the top edge, digital side (XIAO, CAN, power) on the bottom. TX/RX fans are hand-routed on F.Cu, non-crossing, over a solid B.Cu GND plane (no-tracks keepout under the sensing zone). Only the 4-wire I2C/INT/RST bus crosses between the two halves. The remaining nets were routed with a grid router: signals 0.2 mm, +3V3/+3.3VA/CAN_VBUS 0.3 mm, +5V 0.4 mm. The I2C bus needs 6 vias because J7's pin order is the reverse of U6's. Every GND pad reaches the B.Cu pour through a via.

## XIAO pin map

| XIAO pin | GPIO | Function |
|---|---|---|
| D0 / D1 | GPIO1 / GPIO2 | CAN_TX / CAN_RX |
| D2 | GPIO3 | TP_SCL |
| D3 | GPIO4 | TP_SDA |
| D4 | GPIO5 | TP_INT |
| D5 | GPIO6 | TP_RST (active low, 10 kohm pull-up) |
| D6-D9 | GPIO43, 44, 7, 8 | LED HB / SENSE / COMM / FAULT |
| D10 | GPIO9 | free |

I2C is not on the XIAO default pins: use `Wire.begin(4, 3)` (SDA, SCL). D6/D7 are UART0, so the HB/SENSE LEDs flicker with boot logs on reset.

## Files

- `MUCA2.kicad_pro` - project (hierarchical: Power, MCU, Touch AFE, CAN)
- `MUCA2.kicad_sym` - project symbols (FT5316DME from the FocalTech FT5x16 datasheet, XIAO ESP32S3 from the Seeed library)
- `MUCA2.pretty/` - project footprints (XIAO ESP32S3 SMD)

## Open points

- FT5316DME ships blank: flash `MuCaFirmware.bin` over I2C from the ESP32 ([muca-firmware](https://github.com/muca-board/muca-firmware)).
- FT5316DME sourcing not confirmed.
- Verify socket and JST-GH opening direction in the 3D viewer.
- H2 sits at (166.5, 123), 11 mm below H1, to leave room for the RX1 trace to J4 pin 1: the mounting pattern is not a rectangle.
- Silkscreen cleanup (overlapping references), no mounting holes yet.
- CAN bus power limited to 5 V nominal; a 12-24 V bus needs a buck converter.

Related: [Muca_lib](https://github.com/muca-board/Muca), [Muca_experiences](https://github.com/alex-crr/Muca_experiences).
