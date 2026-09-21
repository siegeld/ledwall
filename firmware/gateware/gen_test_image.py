#!/usr/bin/env python3
"""
Generate test images for HUB75 LED panels.
Usage: python3 gen_test_image.py --panel 96x48 --pattern grid --output img_data.bin
"""
import argparse
import struct
import math

PANELS = {
    "256x64": {"columns": 256, "rows": 64},
    "128x64": {"columns": 128, "rows": 64},
    "96x48":  {"columns": 96,  "rows": 48},
    "64x32":  {"columns": 64,  "rows": 32},
    "64x64":  {"columns": 64,  "rows": 64},
}

PATTERNS = ["grid", "rainbow", "solid_white", "solid_red", "solid_green", "solid_blue"]


def create_header(columns, rows):
    """Create image header."""
    total_pixels = columns * rows
    data = bytearray(256 + total_pixels * 4)
    struct.pack_into('<I', data, 0, columns)  # width
    struct.pack_into('<I', data, 4, total_pixels)  # length (total pixels)
    struct.pack_into('<I', data, 8, 0xd1581a40)  # magic
    struct.pack_into('<I', data, 12, 0xda5a0001)  # magic
    return data


def set_pixel(data, columns, row, col, r, g, b):
    """Set a pixel at (row, col) to RGB color."""
    HEADER_SIZE = 256
    offset = HEADER_SIZE + row * columns * 4 + col * 4
    # Little-endian RGBX format: 0x00BBGGRR
    data[offset:offset+4] = struct.pack('<I', (b << 16) | (g << 8) | r)


def hsv_to_rgb(h, s, v):
    """Convert HSV to RGB. h in [0,360), s,v in [0,1]."""
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c

    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x

    return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)


# Simple 5x7 pixel font for digits and basic chars
FONT_5X7 = {
    '0': ["01110","10001","10011","10101","11001","10001","01110"],
    '1': ["00100","01100","00100","00100","00100","00100","01110"],
    '2': ["01110","10001","00001","00010","00100","01000","11111"],
    '3': ["11111","00010","00100","00010","00001","10001","01110"],
    '4': ["00010","00110","01010","10010","11111","00010","00010"],
    '5': ["11111","10000","11110","00001","00001","10001","01110"],
    '6': ["00110","01000","10000","11110","10001","10001","01110"],
    '7': ["11111","00001","00010","00100","01000","01000","01000"],
    '8': ["01110","10001","10001","01110","10001","10001","01110"],
    '9': ["01110","10001","10001","01111","00001","00010","01100"],
    '.': ["00000","00000","00000","00000","00000","01100","01100"],
    'v': ["00000","00000","10001","10001","10001","01010","00100"],
    'V': ["10001","10001","10001","10001","01010","01010","00100"],
    ' ': ["00000","00000","00000","00000","00000","00000","00000"],
}

def draw_text(data, columns, rows, text, start_x, start_y, color):
    """Draw text using 5x7 pixel font."""
    x = start_x
    for char in text:
        if char in FONT_5X7:
            glyph = FONT_5X7[char]
            for dy, row_bits in enumerate(glyph):
                for dx, bit in enumerate(row_bits):
                    if bit == '1':
                        px, py = x + dx, start_y + dy
                        if 0 <= px < columns and 0 <= py < rows:
                            set_pixel(data, columns, py, px, *color)
            x += 6  # 5 pixels + 1 spacing


def pattern_grid(data, columns, rows, version=None):
    """Grid pattern with horizontal, vertical, and diagonal lines."""
    # Colors
    WHITE = (255, 255, 255)
    RED = (255, 0, 0)
    GREEN = (0, 255, 0)
    BLUE = (0, 0, 255)
    YELLOW = (255, 255, 0)
    MAGENTA = (255, 0, 255)
    CYAN = (0, 255, 255)

    # Horizontal lines - equally spaced
    horizontal_rows = [0, rows // 4, rows // 2, 3 * rows // 4, rows - 1]
    for row in horizontal_rows:
        for col in range(columns):
            set_pixel(data, columns, row, col, *WHITE)

    # Vertical lines - evenly spaced with colors
    vertical_lines = [
        (0, RED),
        (columns // 4, GREEN),
        (columns // 2, BLUE),
        (3 * columns // 4, YELLOW),
        (columns - 1, MAGENTA),
    ]
    for col, color in vertical_lines:
        for row in range(rows):
            set_pixel(data, columns, row, col, *color)

    # Diagonal X pattern
    for row in range(rows):
        col = int(row * columns / rows)
        if 0 <= col < columns:
            set_pixel(data, columns, row, col, *CYAN)
        col = columns - 1 - int(row * columns / rows)
        if 0 <= col < columns:
            set_pixel(data, columns, row, col, *MAGENTA)

    # Draw version text in second-row left square (avoids diagonals)
    if version:
        text_width = len(version) * 6
        square_left = 1
        square_right = columns // 4 - 1
        square_top = rows // 4 + 1
        square_bottom = rows // 2 - 1
        start_x = square_left + (square_right - square_left - text_width) // 2
        start_y = square_top + (square_bottom - square_top - 7) // 2
        draw_text(data, columns, rows, version, start_x, start_y, WHITE)
        print(f"  Version: {version}")

    print(f"  Pattern: grid")
    print(f"  Horizontal lines at rows: {horizontal_rows}")
    print(f"  Vertical lines at cols: {[c for c, _ in vertical_lines]}")
    print(f"  Diagonal X pattern")


def pattern_rainbow(data, columns, rows):
    """Classic rainbow wave pattern - diagonal color waves."""
    for row in range(rows):
        for col in range(columns):
            # Create diagonal waves - hue varies with position
            # Wave moves diagonally across the panel
            hue = ((col + row) * 360 / (columns + rows) * 2) % 360
            r, g, b = hsv_to_rgb(hue, 1.0, 1.0)
            set_pixel(data, columns, row, col, r, g, b)

    print(f"  Pattern: rainbow (diagonal waves)")


def pattern_solid(data, columns, rows, r, g, b, name):
    """Solid color fill."""
    for row in range(rows):
        for col in range(columns):
            set_pixel(data, columns, row, col, r, g, b)
    print(f"  Pattern: solid {name}")


def get_cargo_version():
    """Read version from Cargo.toml."""
    import os
    cargo_path = os.path.join(os.path.dirname(__file__), '..', 'sw_rust', 'barsign_disp', 'Cargo.toml')
    try:
        with open(cargo_path) as f:
            for line in f:
                if line.startswith('version = '):
                    return line.split('"')[1]
    except:
        pass
    return None


def create_test_image(columns, rows, pattern, output_path, version=None):
    """Create a test pattern for the specified panel size."""
    data = create_header(columns, rows)

    if pattern == "grid":
        pattern_grid(data, columns, rows, version=version)
    elif pattern == "rainbow":
        pattern_rainbow(data, columns, rows)
    elif pattern == "solid_white":
        pattern_solid(data, columns, rows, 255, 255, 255, "white")
    elif pattern == "solid_red":
        pattern_solid(data, columns, rows, 255, 0, 0, "red")
    elif pattern == "solid_green":
        pattern_solid(data, columns, rows, 0, 255, 0, "green")
    elif pattern == "solid_blue":
        pattern_solid(data, columns, rows, 0, 0, 255, "blue")
    else:
        raise ValueError(f"Unknown pattern: {pattern}")

    with open(output_path, 'wb') as f:
        f.write(data)

    print(f"Created test image: {output_path}")
    print(f"  Panel: {columns}x{rows}")
    print(f"  Size: {len(data)} bytes")


def main():
    parser = argparse.ArgumentParser(
        description="Generate HUB75 test images",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available patterns:
  grid         White grid lines with colored verticals and diagonal X (default)
  rainbow      Classic rainbow diagonal wave pattern
  solid_white  Solid white (full brightness test)
  solid_red    Solid red
  solid_green  Solid green
  solid_blue   Solid blue

Examples:
  python3 gen_test_image.py --panel 96x48 --pattern grid
  python3 gen_test_image.py --panel 128x64 --pattern rainbow
  python3 gen_test_image.py -p 96x48 -t rainbow -o rainbow.bin
""")
    parser.add_argument("--panel", "-p", choices=PANELS.keys(),
                        help="Panel type (e.g., 96x48, 128x64)")
    parser.add_argument("--pattern", "-t", choices=PATTERNS, default="grid",
                        help="Test pattern (default: grid)")
    parser.add_argument("--columns", type=int,
                        help="Panel columns (overrides --panel)")
    parser.add_argument("--rows", type=int,
                        help="Panel rows (overrides --panel)")
    parser.add_argument("--output", "-o", default="img_data.bin",
                        help="Output file (default: img_data.bin)")
    args = parser.parse_args()

    # Get dimensions from --panel or explicit --columns/--rows
    if args.panel:
        columns = PANELS[args.panel]["columns"]
        rows = PANELS[args.panel]["rows"]
    else:
        columns = args.columns if args.columns else 96
        rows = args.rows if args.rows else 48

    # Allow explicit overrides
    if args.columns:
        columns = args.columns
    if args.rows:
        rows = args.rows

    # Get version from Cargo.toml for grid pattern
    version = get_cargo_version()
    if version:
        version = f"v{version}"

    create_test_image(columns, rows, args.pattern, args.output, version=version)


if __name__ == "__main__":
    main()
