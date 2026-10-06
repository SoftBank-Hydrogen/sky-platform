"""Sky's command-line entry point over the imported deployment engine."""

from sky_platform.runtime.server import serve


def main() -> None:
    serve(product_name="Sky", default_state_dir=".sky")
