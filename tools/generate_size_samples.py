"""Generate public synthetic size fixtures; never open their payloads."""
import argparse
import json
from pathlib import Path

from PIL import Image
import qrcode


def qr(text, box_size):
    code = qrcode.QRCode(box_size=box_size, border=4)
    code.add_data(text)
    code.make(fit=True)
    return code.make_image().convert("L")


def generate(output):
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    definitions = (
        ("one_pixel", (320, 240), (("SCREENQR-TINY-PUBLIC", 1, (30, 40)),)),
        ("8k_mixed", (7680, 4320), (("SCREENQR-GIANT-PUBLIC", 112, (40, 40)), ("SCREENQR-TINY-PUBLIC", 1, (7600, 4200)))),
        ("16k_wide", (16384, 1024), (("SCREENQR-16K-PUBLIC", 1, (16300, 950)),)),
        ("same_payload_two_sizes", (3200, 2000), (("SCREENQR-SAME-PUBLIC", 1, (100, 100)), ("SCREENQR-SAME-PUBLIC", 14, (1900, 1100)))),
    )
    for name, size, symbols in definitions:
        with Image.new("L", size, 255) as canvas:
            for text, scale, position in symbols:
                with qr(text, scale) as image:
                    if position[0] + image.width > size[0] or position[1] + image.height > size[1]:
                        raise ValueError("Fixture QR must remain complete")
                    canvas.paste(image, position)
            canvas.save(output / (name + ".png"))
        cases.append(dict(id=name, png=name + ".png", size=list(size),
                          payloads=sorted({s[0] for s in symbols}), instance_count=len(symbols)))
    payload = "SCREENQR-DENSE-SEAM-PUBLIC-" + "A" * 900
    dense = qrcode.QRCode(version=40, box_size=4, border=4)
    dense.add_data(payload)
    dense.make(fit=False)
    with Image.new("L", (12000, 4000), 255) as canvas:
        with dense.make_image().convert("L") as image:
            canvas.paste(image, (1000, 1000))
        canvas.save(output / "dense_seam.png")
    cases.append(dict(id="dense_seam", png="dense_seam.png", size=[12000, 4000],
                      payloads=[payload], instance_count=1))
    (output / "manifest.json").write_text(json.dumps(dict(
        source="public synthetic size samples; not business codes", cases=cases),
        ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"Size fixtures generated: {len(cases)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    generate(parser.parse_args().output)
