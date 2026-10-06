"""Command-line entry point for the Sky control plane."""

from interfaces.http.server import serve


def main() -> None:
    serve(product_name="Sky", default_state_dir=".sky")
