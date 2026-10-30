"""Entry point for the standalone `gtu` binaries built with PyInstaller."""

from gpu_token_utilization.cli import app

if __name__ == "__main__":
    app(prog_name="gtu")
