"""Sky's command-line entry point over the imported deployment engine."""

from api.server import serve


def main() -> None:
    serve(product_name="Sky", default_state_dir=".sky")


if __name__ == "__main__":
    main()
