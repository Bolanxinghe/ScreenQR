# Third-party notices

ScreenQR original application code is MIT licensed. It does not redistribute AndroMeld or its decompiled code. Runtime dependencies retain their respective licenses; full notices are in `licenses/`.

| Component | Version / source | License notice |
| --- | --- | --- |
| Python runtime | 3.12.10, python.org Windows x64 distribution | Python-3.12.txt (PSF and bundled-component terms) |
| Tcl / Tk | 8.6 runtime included by Python | Tcl-8.6.txt, Tk-8.6.txt |
| Pillow | 12.3.0 official PyPI wheel | Pillow-12.3.0.txt, including bundled codec notices |
| ZXing-C++ Python module | 3.1.1 official PyPI wheel | ZXing-Cpp-3.1.1.txt, Apache-2.0 |
| libzint | Embedded in ZXing-C++'s new writer backend | libzint-BSD.txt, BSD-3-Clause |
| nanobind | 2.11.0, specified by ZXing-C++ Python build | nanobind-2.11.0.txt, BSD-3-Clause |
| PyInstaller bootloader / build tool | 6.22.2 | PyInstaller-6.22.2.txt, GPL with distribution exception |

PyInstaller's exception allows the generated application to use its chosen license, subject to each bundled dependency's terms. qrcode 8.2 is a development/test dependency only and is not imported or shipped in the frozen application.

The local dependency notices were obtained from the installed official distributions, the ZXing-C++ 3.1.1 source distribution, [nanobind v2.11.0](https://github.com/wjakob/nanobind/blob/v2.11.0/LICENSE), and [Tcl 8.6](https://github.com/tcltk/tcl/blob/core-8-6-branch/license.terms). Build tools and wheel versions are pinned in the requirements files. No upstream trademark or endorsement is claimed.
